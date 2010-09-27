import py
from xdist.remote import SlaveController
from xdist.remote import serialize_report, unserialize_report
import execnet
queue = py.builtin._tryimport("queue", "Queue")
from py.builtin import print_
import marshal

def check_marshallable(d):
    try:
        marshal.dumps(d)
    except ValueError:
        py.std.pprint.pprint(d)
        raise ValueError("not marshallable")

class EventCall:
    def __init__(self, eventcall):
        self.name, self.kwargs = eventcall

    def __str__(self):
        return "<EventCall %s(**%s)>" %(self.name, self.kwargs)

class SlaveSetup:
    use_callback = False

    def __init__(self, request):
        self.testdir = testdir = request.getfuncargvalue("testdir")
        self.request = request
        self.events = queue.Queue()

    def setup(self, ):
        self.testdir.chdir()
        #import os ; os.environ['EXECNET_DEBUG'] = "2"
        self.gateway = execnet.makegateway()
        self.config = config = self.testdir.parseconfig()
        putevent = self.use_callback and self.events.put or None
        self.slp = SlaveController(None, self.gateway, config, putevent)
        self.request.addfinalizer(self.slp.ensure_teardown)
        self.slp.setup()

    def popevent(self, name=None):
        while 1:
            if self.use_callback:
                data = self.events.get(timeout=2)
            else:
                data = self.slp.channel.receive(timeout=2)
            ev = EventCall(data)
            if name is None or ev.name == name:
                return ev
            print("skipping %s" % (ev,))

    def sendcommand(self, name, **kwargs):
        self.slp.sendcommand(name, **kwargs)

def pytest_funcarg__slave(request):
    return SlaveSetup(request)

def test_remoteinitconfig(testdir):
    from xdist.remote import remote_initconfig
    config1 = testdir.parseconfig()
    config2 = testdir.parseconfig("-x")
    cfg = remote_initconfig(config2, config1.option.__dict__, config1.args)
    assert cfg == config2
    assert cfg.option.__dict__ == config1.option.__dict__

class TestReportSerialization:
    def test_itemreport_outcomes(self, testdir):
        reprec = testdir.inline_runsource("""
            import py
            def test_pass(): pass
            def test_fail(): 0/0
            @py.test.mark.skipif("True")
            def test_skip(): pass
            def test_skip_imperative():
                py.test.skip("hello")
            @py.test.mark.xfail("True")
            def test_xfail(): 0/0
            def test_xfail_imperative():
                py.test.xfail("hello")
        """)
        reports = reprec.getreports("pytest_runtest_logreport")
        assert len(reports) == 6
        for rep in reports:
            d = serialize_report(rep)
            check_marshallable(d)
            newrep = unserialize_report(d)
            assert newrep.passed == rep.passed
            assert newrep.failed == rep.failed
            assert newrep.skipped == rep.skipped
            assert newrep.outcome == rep.outcome
            assert newrep.when == rep.when
            assert newrep.keywords == rep.keywords
            if rep.failed:
                assert newrep.longrepr == str(rep.longrepr)

    def test_collectreport_passed(self, testdir):
        reprec = testdir.inline_runsource("def test_func(): pass")
        reports = reprec.getreports("pytest_collectreport")
        for rep in reports:
            d = serialize_report(rep)
            check_marshallable(d)
            newrep = unserialize_report(d)
            assert newrep.passed == rep.passed
            assert newrep.failed == rep.failed
            assert newrep.skipped == rep.skipped

    def test_collectreport_fail(self, testdir):
        reprec = testdir.inline_runsource("qwe abc")
        reports = reprec.getreports("pytest_collectreport")
        assert reports
        for rep in reports:
            d = serialize_report(rep)
            check_marshallable(d)
            newrep = unserialize_report(d)
            assert newrep.passed == rep.passed
            assert newrep.failed == rep.failed
            assert newrep.skipped == rep.skipped
            if rep.failed:
                assert newrep.longrepr == str(rep.longrepr)

class TestSlaveInteractor:
    def test_basic_collect_and_runtests(self, slave):
        p = slave.testdir.makepyfile("""
            def test_func():
                pass
        """)
        slave.setup()
        ev = slave.popevent()
        assert ev.name == "slaveready"
        ev = slave.popevent()
        assert ev.name == "collectionstart"
        assert not ev.kwargs
        ev = slave.popevent("collectionfinish")
        assert ev.kwargs['topdir'] == slave.testdir.tmpdir
        ids = ev.kwargs['ids']
        assert len(ids) == 1
        slave.sendcommand("runtests", ids=ids)
        ev = slave.popevent("testreport")
        assert ev.name == "testreport"
        rep = unserialize_report(ev.kwargs['data'])
        assert rep.nodeid.endswith("::test_func")
        assert rep.passed
        assert rep.when == "call"
        slave.sendcommand("shutdown")
        ev = slave.popevent("slavefinished")
        assert 'slaveoutput' in ev.kwargs

    def test_remote_collect_skip(self, slave):
        p = slave.testdir.makepyfile("""
            import py
            py.test.skip("hello")
        """)
        slave.setup()
        ev = slave.popevent("collectionstart")
        assert not ev.kwargs
        ev = slave.popevent()
        assert ev.name == "collectreport"
        rep = unserialize_report(ev.kwargs['data'])
        assert rep.skipped
        ev = slave.popevent("collectionfinish")
        print ev.kwargs
        assert not ev.kwargs['ids']

    def test_remote_collect_fail(self, slave):
        p = slave.testdir.makepyfile("""aasd qwe""")
        slave.setup()
        ev = slave.popevent("collectionstart")
        assert not ev.kwargs
        ev = slave.popevent()
        assert ev.name == "collectreport"
        rep = unserialize_report(ev.kwargs['data'])
        assert rep.failed
        ev = slave.popevent("collectionfinish")
        print ev.kwargs
        assert not ev.kwargs['ids']

    def test_happy_run_events_converted(self, testdir, slave):
        py.test.xfail("implement a simple test for event production")
        assert not slave.use_callback
        p = slave.testdir.makepyfile("""
            def test_func():
                pass
        """)
        slave.setup()
        hookrec = testdir.getreportrecorder(slave.config)
        for data in slave.slp.channel:
            slave.slp.process_from_remote(data)
        slave.slp.process_from_remote(slave.slp.ENDMARK)
        py.std.pprint.pprint(hookrec.hookrecorder.calls)
        hookrec.hookrecorder.contains([
            ("pytest_collectstart", "collector.fspath == aaa"),
            ("pytest_pycollect_makeitem", "name == 'test_func'"),
            ("pytest_collectreport", "report.collector.fspath == aaa"),
            ("pytest_collectstart", "collector.fspath == bbb"),
            ("pytest_pycollect_makeitem", "name == 'test_func'"),
            ("pytest_collectreport", "report.collector.fspath == bbb"),
        ])
