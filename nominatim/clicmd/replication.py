"""
Implementation of the 'replication' sub-command.
"""
import datetime as dt
import logging
import socket
import time

from ..db import status
from ..db.connection import connect
from ..errors import UsageError

LOG = logging.getLogger()

# Do not repeat documentation of subcommand classes.
# pylint: disable=C0111
# Using non-top-level imports to make pyosmium optional for replication only.
# pylint: disable=E0012,C0415

def _osm2pgsql_options_from_args(args, default_cache, default_threads):
    """ Set up the standard osm2pgsql from the command line arguments.
    """
    return dict(osm2pgsql=args.osm2pgsql_path,
                osm2pgsql_cache=args.osm2pgsql_cache or default_cache,
                osm2pgsql_style=args.config.get_import_style_file(),
                threads=args.threads or default_threads,
                dsn=args.config.get_libpq_dsn(),
                flatnode_file=args.config.FLATNODE_FILE)


class UpdateReplication:
    """\
    Update the database using an online replication service.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Arguments for initialisation')
        group.add_argument('--init', action='store_true',
                           help='Initialise the update process')
        group.add_argument('--no-update-functions', dest='update_functions',
                           action='store_false',
                           help="""Do not update the trigger function to
                                   support differential updates.""")
        group = parser.add_argument_group('Arguments for updates')
        group.add_argument('--check-for-updates', action='store_true',
                           help='Check if new updates are available and exit')
        group.add_argument('--once', action='store_true',
                           help="""Download and apply updates only once. When
                                   not set, updates are continuously applied""")
        group.add_argument('--no-index', action='store_false', dest='do_index',
                           help="""Do not index the new data. Only applicable
                                   together with --once""")
        group.add_argument('--osm2pgsql-cache', metavar='SIZE', type=int,
                           help='Size of cache to be used by osm2pgsql (in MB)')
        group = parser.add_argument_group('Download parameters')
        group.add_argument('--socket-timeout', dest='socket_timeout', type=int, default=60,
                           help='Set timeout for file downloads.')

    @staticmethod
    def _init_replication(args):
        from ..tools import replication, refresh

        LOG.warning("Initialising replication updates")
        conn = connect(args.config.get_libpq_dsn())
        replication.init_replication(conn, base_url=args.config.REPLICATION_URL)
        if args.update_functions:
            LOG.warning("Create functions")
            refresh.create_functions(conn, args.config, args.data_dir,
                                     True, False)
        conn.close()
        return 0


    @staticmethod
    def _check_for_updates(args):
        from ..tools import replication

        conn = connect(args.config.get_libpq_dsn())
        ret = replication.check_for_updates(conn, base_url=args.config.REPLICATION_URL)
        conn.close()
        return ret

    @staticmethod
    def _report_update(batchdate, start_import, start_index):
        def round_time(delta):
            return dt.timedelta(seconds=int(delta.total_seconds()))

        end = dt.datetime.now(dt.timezone.utc)
        LOG.warning("Update completed. Import: %s. %sTotal: %s. Remaining backlog: %s.",
                    round_time((start_index or end) - start_import),
                    "Indexing: {} ".format(round_time(end - start_index))
                    if start_index else '',
                    round_time(end - start_import),
                    round_time(end - batchdate))

    @staticmethod
    def _update(args):
        from ..tools import replication
        from ..indexer.indexer import Indexer

        params = _osm2pgsql_options_from_args(args, 2000, 1)
        params.update(base_url=args.config.REPLICATION_URL,
                      update_interval=args.config.get_int('REPLICATION_UPDATE_INTERVAL'),
                      import_file=args.project_dir / 'osmosischange.osc',
                      max_diff_size=args.config.get_int('REPLICATION_MAX_DIFF'),
                      indexed_only=not args.once)

        # Sanity check to not overwhelm the Geofabrik servers.
        if 'download.geofabrik.de'in params['base_url']\
           and params['update_interval'] < 86400:
            LOG.fatal("Update interval too low for download.geofabrik.de.\n"
                      "Please check install documentation "
                      "(https://nominatim.org/release-docs/latest/admin/Import-and-Update#"
                      "setting-up-the-update-process).")
            raise UsageError("Invalid replication update interval setting.")

        if not args.once:
            if not args.do_index:
                LOG.fatal("Indexing cannot be disabled when running updates continuously.")
                raise UsageError("Bad argument '--no-index'.")
            recheck_interval = args.config.get_int('REPLICATION_RECHECK_INTERVAL')

        while True:
            conn = connect(args.config.get_libpq_dsn())
            start = dt.datetime.now(dt.timezone.utc)
            state = replication.update(conn, params)
            if state is not replication.UpdateState.NO_CHANGES:
                status.log_status(conn, start, 'import')
            batchdate, _, _ = status.get_status(conn)
            conn.close()

            if state is not replication.UpdateState.NO_CHANGES and args.do_index:
                index_start = dt.datetime.now(dt.timezone.utc)
                indexer = Indexer(args.config.get_libpq_dsn(),
                                  args.threads or 1)
                indexer.index_boundaries(0, 30)
                indexer.index_by_rank(0, 30)

                conn = connect(args.config.get_libpq_dsn())
                status.set_indexed(conn, True)
                status.log_status(conn, index_start, 'index')
                conn.close()
            else:
                index_start = None

            if LOG.isEnabledFor(logging.WARNING):
                UpdateReplication._report_update(batchdate, start, index_start)

            if args.once:
                break

            if state is replication.UpdateState.NO_CHANGES:
                LOG.warning("No new changes. Sleeping for %d sec.", recheck_interval)
                time.sleep(recheck_interval)

        return state.value


    @staticmethod
    def run(args):
        socket.setdefaulttimeout(args.socket_timeout)

        if args.init:
            return UpdateReplication._init_replication(args)

        if args.check_for_updates:
            return UpdateReplication._check_for_updates(args)

        return UpdateReplication._update(args)
