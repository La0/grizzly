# coding=utf-8
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""
Grizzly is a general purpose browser fuzzer made of up of multiple modules. The
intention is to create a platform that can be extended by the creation of corpus
managers to fuzz different components of the browsers.

Grizzly is not meant to be much more than the automation glue code between
the modules.

A corpus manager is used to wrap an existing fuzzer to allow it to be run with
grizzly. Corpus managers take the content output by fuzzers and transform it
into a format that can be served to and processed by a browser.

Support for different browser can be added by the creation of a browser "puppet"
module (see ffpuppet). TODO: Implement generic "puppet" support.
"""

import logging
import os
import shutil
import tempfile

from ffpuppet import BrowserTerminatedError, BrowserTimeoutError, LaunchError
import sapphire

from .args import GrizzlyArgs
from .corpman import adapters, IOManager
from .corpman.storage import TestFile
from .reporter import FilesystemReporter, FuzzManagerReporter, S3FuzzManagerReporter
from .status import Status
from .target import load as load_target


__author__ = "Tyson Smith"
__credits__ = ["Tyson Smith", "Jesse Schwartzentruber"]


log = logging.getLogger("grizzly")  # pylint: disable=invalid-name


class Session(object):
    EXIT_SUCCESS = 0
    EXIT_ERROR = 1
    EXIT_ABORT = 3
    EXIT_LAUNCH_FAILURE = 7
    FM_LOG_SIZE_LIMIT = 0x40000  # max log size for log sent to FuzzManager (256KB)
    TARGET_LOG_SIZE_WARN = 0x1900000  # display warning when target log files exceed limit (25MB)

    def __init__(self, adapter, coverage, ignore, iomanager, reporter, target):
        self.adapter = adapter
        self.coverage = coverage
        self.ignore = ignore  # TODO: this should be part of the reporter
        self.iomanager = iomanager
        self.reporter = reporter
        self.server = None
        self.status = Status.start()
        self.target = target

    def check_results(self, unserved, was_timeout):
        # attempt to detect a failure
        failure_detected = self.target.detect_failure(self.ignore, was_timeout)
        if unserved and self.adapter.IGNORE_UNSERVED:
            # if nothing was served remove most recent
            # test case from list to help maintain browser/fuzzer sync
            log.info("Ignoring test case since nothing was served")
            self.iomanager.tests.pop().cleanup()
        # handle failure if detected
        if failure_detected == self.target.RESULT_FAILURE:
            self.status.results += 1
            log.info("Result detected")
            self.report_result()
        elif failure_detected == self.target.RESULT_IGNORED:
            self.status.ignored += 1
            log.info("Ignored (%d)", self.status.ignored)

    def config_server(self, iteration_timeout):
        assert self.server is None
        log.debug("starting sapphire server")
        # have client error pages (code 4XX) call window.close() after a few seconds
        sapphire.Sapphire.CLOSE_CLIENT_ERROR = 1
        # launch http server used to serve test cases
        self.server = sapphire.Sapphire(timeout=iteration_timeout)
        # add include paths to server
        for url_path, target_path in self.iomanager.server_map.includes:
            self.server.add_include(url_path, target_path)
        # add dynamic responses to the server
        for dyn_rsp in self.iomanager.server_map.dynamic_responses:
            self.server.add_dynamic_response(
                dyn_rsp["url"],
                dyn_rsp["callback"],
                mime_type=dyn_rsp["mime"])
        def _dyn_resp_close():
            self.target.close()
            return b"<h1>Close Browser</h1>"
        self.server.add_dynamic_response("/close_browser", _dyn_resp_close, mime_type="text/html")

    def close(self):
        self.status.cleanup()
        if self.server is not None:
            self.server.close()

    def generate_testcase(self, dump_path):
        assert self.server is not None
        log.debug("calling iomanager.create_testcase()")
        test = self.iomanager.create_testcase(self.adapter.NAME, rotation_period=self.adapter.ROTATION_PERIOD)
        log.debug("calling self.adapter.generate()")
        self.adapter.generate(test, self.iomanager.active_input, self.iomanager.server_map)
        if self.target.prefs is not None:
            test.add_meta(TestFile.from_file(self.target.prefs, "prefs.js"))
        # update sapphire redirects from the adapter
        for redirect in self.iomanager.server_map.redirects:
            self.server.set_redirect(redirect["url"], redirect["file_name"], redirect["required"])
        # dump test case files to filesystem to be served
        test.dump(dump_path)
        return test

    def launch_target(self):
        assert self.target.closed
        launch_timeouts = 0
        while True:
            try:
                log.info("Launching target")
                self.target.launch(self.location)
            except BrowserTerminatedError:
                # this result likely has nothing to do with Grizzly
                self.status.results += 1
                log.error("Launch error detected")
                self.report_result()
                raise
            except BrowserTimeoutError:
                launch_timeouts += 1
                log.warning("Launch timeout detected (attempt #%d)", launch_timeouts)
                # likely has nothing to do with Grizzly but is seen frequently on machines under a high load
                # after 3 timeouts in a row something is likely wrong so raise
                if launch_timeouts < 3:
                    continue
                raise
            break

    @property
    def location(self):
        assert self.server is not None
        location = ["http://127.0.0.1:%d/" % self.server.get_port(), self.iomanager.landing_page()]
        if self.iomanager.harness is not None:
            location.append("?timeout=%d" % (self.adapter.TEST_DURATION * 1000))
            location.append("&close_after=%d" % self.target.rl_reset)
            if not self.target.forced_close:
                location.append("&forced_close=0")
        return "".join(location)

    def report_result(self):
        # create working directory for current testcase
        result_logs = tempfile.mkdtemp(prefix="grz_logs_", dir=self.iomanager.working_path)
        self.target.save_logs(result_logs, meta=True)
        log.info("Reporting results...")
        self.iomanager.tests.reverse()  # order test cases newest to oldest
        self.reporter.submit(result_logs, self.iomanager.tests)
        if os.path.isdir(result_logs):
            shutil.rmtree(result_logs)
        self.iomanager.purge_tests()

    def run(self, iteration_limit=None):
        assert self.server is not None, "server is not configured"
        while True:  # main fuzzing loop
            self.status.report()
            self.status.iteration += 1

            if self.target.closed:
                self.launch_target()
            self.target.step()

            # create and populate a test case
            wwwdir = tempfile.mkdtemp(prefix="grz_test_", dir=self.iomanager.working_path)
            try:
                current_test = self.generate_testcase(wwwdir)
                # print iteration status
                if self.iomanager.active_input is None:
                    active_file = None
                else:
                    active_file = self.iomanager.active_input.file_name
                if not self.adapter.ROTATION_PERIOD:
                    log.info(
                        "[I%04d-L%02d-R%02d] %s",
                        self.status.iteration,
                        self.adapter.size(),
                        self.status.results,
                        os.path.basename(active_file))
                else:
                    if active_file and self.status.test_name != active_file:
                        self.status.test_name = active_file
                        log.info("Now fuzzing: %s", os.path.basename(active_file))
                    log.info("I%04d-R%02d ", self.status.iteration, self.status.results)
                # use Sapphire to serve the most recent test case
                server_status, files_served = self.server.serve_path(
                    wwwdir,
                    continue_cb=self.target.monitor.is_healthy,
                    optional_files=current_test.get_optional())
            finally:
                # remove test case working directory
                if os.path.isdir(wwwdir):
                    shutil.rmtree(wwwdir)
            if server_status == sapphire.SERVED_TIMEOUT:
                log.debug("calling self.adapter.on_timeout()")
                self.adapter.on_timeout(current_test, files_served)
            else:
                log.debug("calling self.adapter.on_served()")
                self.adapter.on_served(current_test, files_served)

            # check for results and report as necessary
            self.check_results(not files_served, server_status == sapphire.SERVED_TIMEOUT)

            # warn about large browser logs
            self.status.log_size = self.target.log_size()
            if self.status.log_size > self.TARGET_LOG_SIZE_WARN:
                log.warning("Large browser logs: %dMBs", (self.status.log_size/0x100000))

            if self.coverage:
                self.target.dump_coverage()

            # trigger relaunch by closing the browser if needed
            self.target.check_relaunch()

            # all test cases have been replayed
            if not self.adapter.ROTATION_PERIOD and not self.adapter.size():
                log.info("Replay Complete")
                break

            if iteration_limit is not None and self.status.iteration == iteration_limit:
                log.info("Hit iteration limit")
                break


def console_init_logging():
    log_level = logging.INFO
    log_fmt = "[%(asctime)s] %(message)s"
    if bool(os.getenv("DEBUG")):
        log_level = logging.DEBUG
        log_fmt = "%(levelname).1s %(name)s [%(asctime)s] %(message)s"
    logging.basicConfig(format=log_fmt, datefmt="%Y-%m-%d %H:%M:%S", level=log_level)


def console_main():
    console_init_logging()
    return main(GrizzlyArgs().parse_args())


def main(args):
    # NOTE: grizzly.reduce.reduce.main mirrors this pretty closely
    #       please check if updates here should go there too
    log.info("Starting Grizzly")
    if args.fuzzmanager:
        FuzzManagerReporter.sanity_check(args.binary)
    elif args.s3_fuzzmanager:
        S3FuzzManagerReporter.sanity_check(args.binary)

    if args.ignore:
        log.info("Ignoring: %s", ", ".join(args.ignore))
    if args.xvfb:
        log.info("Running with Xvfb")
    if args.valgrind:
        log.info("Running with Valgrind. This will be SLOW!")
    if args.rr:
        log.info("Running with RR")

    adapter = None
    iomanager = None
    session = None
    target = None
    try:
        log.debug("initializing the IOManager")
        iomanager = IOManager(
            report_size=(max(args.cache, 0) + 1),
            mime_type=args.mime,
            working_path=args.working_path)

        log.debug("initializing the Adapter")
        adapter = adapters.get(args.adapter)()

        if adapter.TEST_DURATION >= args.timeout:
            raise RuntimeError("Test duration (%ds) should be less than browser timeout (%ds)" % (
                adapter.TEST_DURATION, args.timeout))

        if args.input:
            iomanager.scan_input(
                args.input,
                accepted_extensions=args.accepted_extensions,
                sort=adapter.ROTATION_PERIOD == 0)
        log.info("Found %d test case(s)", iomanager.size())

        if adapter.ROTATION_PERIOD == 0:
            log.info("Running in SINGLE PASS mode")
        elif args.coverage:
            log.info("Running in COVERAGE mode")
            # cover as many test cases as possible
            adapter.ROTATION_PERIOD = 1
        else:
            log.info("Running in FUZZING mode")

        log.debug("initializing the Target")
        target = load_target(args.platform)(
            args.binary,
            args.extension,
            args.launch_timeout,
            args.log_limit,
            args.memory,
            args.prefs,
            args.relaunch,
            rr=args.rr,
            valgrind=args.valgrind,
            xvfb=args.xvfb)
        adapter.monitor = target.monitor
        if args.soft_asserts:
            target.add_abort_token("###!!! ASSERTION:")

        log.debug("calling adapter setup()")
        adapter.setup(iomanager.server_map)
        log.debug("configuring harness")
        iomanager.harness = adapter.get_harness()

        log.debug("initializing the Reporter")
        if args.fuzzmanager:
            log.info("Results will be reported via FuzzManager")
            reporter = FuzzManagerReporter(args.binary, tool=args.tool)
        elif args.s3_fuzzmanager:
            log.info("Results will be reported via FuzzManager w/ large attachments in S3")
            reporter = S3FuzzManagerReporter(args.binary, tool=args.tool)
        else:
            reporter = FilesystemReporter()
            log.info("Results will be stored in %r", reporter.report_path)

        log.debug("initializing the Session")
        session = Session(
            adapter,
            args.coverage,
            args.ignore,
            iomanager,
            reporter,
            target)

        session.config_server(args.timeout)

        session.run()

    except KeyboardInterrupt:
        return Session.EXIT_ABORT

    except LaunchError:
        return Session.EXIT_LAUNCH_FAILURE

    finally:
        log.warning("Shutting down...")
        if session is not None:
            session.close()
        if target is not None:
            target.cleanup()
        if adapter is not None:
            adapter.cleanup()
        if iomanager is not None:
            iomanager.cleanup()

    return Session.EXIT_SUCCESS
