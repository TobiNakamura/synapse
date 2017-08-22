import os
import ssl
import sys
import shutil
import socket
import logging
import tempfile
import unittest
import threading
import contextlib

logging.basicConfig(level=logging.WARNING)

import synapse.link as s_link
import synapse.compat as s_compat
import synapse.cortex as s_cortex
import synapse.eventbus as s_eventbus

import synapse.cores.common as s_cores_common

import synapse.lib.scope as s_scope
import synapse.lib.ingest as s_ingest
import synapse.lib.output as s_output
import synapse.lib.thishost as s_thishost

from synapse.common import *

# create the global multi-plexor *not* within a test
# to avoid "leaked resource" when a test triggers creation
s_scope.get('plex')

class TooFewEvents(Exception): pass

# Py2/3 SSL Exception Compat
if s_compat.version >= (3, 0, 0):
    TestSSLInvalidClientCertErr = ssl.SSLError
    TestSSLConnectionResetErr = ConnectionResetError
else:
    TestSSLInvalidClientCertErr = socket.error
    TestSSLConnectionResetErr = socket.error

class TestEnv:

    def __init__(self):
        self.items = {}
        self.tofini = []

    def __getattr__(self, prop):
        item = self.items.get(prop)
        if item is None:
            raise AttributeError(prop)
        return item

    def __enter__(self):
        return self

    def __exit__(self, cls, exc, tb):
        self.fini()

    def add(self, name, item, fini=False):
        self.items[name] = item
        if fini:
            self.tofini.append(item)

    def fini(self):
        for bus in self.tofini:
            bus.fini()


class TestOutPut(s_output.OutPutStr):

    def expect(self, substr):
        outs = str(self)
        if outs.find(substr) == -1:
            raise Exception('TestOutPut.expect(%s) not in %s' % (substr, outs))

class SynTest(unittest.TestCase):

    def getTestWait(self, bus, size, *evts):
        return s_eventbus.Waiter(bus, size, *evts)

    def skipIfOldPython(self):
        python_version = sys.version_info
        if python_version.major == 2 or (python_version.major == 3 and python_version.minor < 3):
            raise unittest.SkipTest('old python version')

    def skipIfNoInternet(self):
        if os.getenv('SYN_TEST_NO_INTERNET'):
            raise unittest.SkipTest('no internet access')

    def getPgConn(self):
        '''
        Get a psycopg2 connection object.

        The PG database connected to is derived from the SYN_TEST_PG_DB
        environmental variable.

        Returns:
            psycopg2.connection: Raw psycopg2 connection object.

        '''
        db = os.getenv('SYN_TEST_PG_DB')
        if not db:
            raise unittest.SkipTest('no SYN_TEST_PG_DB envar')
        try:
            import psycopg2
        except ImportError:
            raise unittest.SkipTest('psycopg2 not installed.')

        url = 'postgres://%s' % db
        link = s_link.chopLinkUrl(url)

        def _initDbInfo(link):

            dbinfo = {}

            path = link[1].get('path')
            if path:
                parts = [p for p in path.split('/') if p]
                if parts:
                    dbinfo['database'] = parts[0]

            host = link[1].get('host')
            if host is not None:
                dbinfo['host'] = host

            port = link[1].get('port')
            if port is not None:
                dbinfo['port'] = port

            user = link[1].get('user')
            if user is not None:
                dbinfo['user'] = user

            passwd = link[1].get('passwd')
            if passwd is not None:
                dbinfo['password'] = passwd

            return dbinfo

        dbinfo = _initDbInfo(link)
        conn = psycopg2.connect(**dbinfo)
        return conn

    def getPgCore(self, table='', persist=False, **opts):
        '''
        Get a Postgresql backed Cortex.

        This will grab the SYN_TEST_PG_DB environmental variable, and use it to construct
        a string to connect to a PSQL server and create a Cortex. By default, the Cortex
        DB tables will be dropped when onfini() is called on the Cortex.

        Some example values for this envar are shown below::

            # From our .drone.yml file
            root@database:5432/syn_test
            # An example which may be used with a local docker image
            # after having created the syn_test database
            postgres:1234@localhost:5432/syn_test

        Args:
            table (str): The PSQL table name to use.  If the table name is not provided
                         by URL or argument; a random table name will be created.
            persist (bool): If set to True, keep the tables created by the Cortex creation.
            opts: Additional options passed to openlink call.

        Returns:
            A PSQL backed cortex.

        Raises:
            unittest.SkipTest: if there is no SYN_TEST_PG_DB envar set.
        '''
        db = os.getenv('SYN_TEST_PG_DB')
        if not db:
            raise unittest.SkipTest('no SYN_TEST_PG_DB envar')

        if not table:
            table = 'syn_test_%s' % guid()
        core = s_cortex.openurl('postgres://%s/%s' % (db, table), **opts)

        def droptable():
            with core.getCoreXact() as xact:
                xact.cursor.execute('DROP TABLE %s' % (table,))
                xact.cursor.execute('DROP TABLE IF EXISTS %s' % (table + '_blob',))

        if not persist:
            core.onfini(droptable)
        return core

    def getRev0DbByts(self):
        '''
        Get the bytes for the rev0.db SQLIte Cortex.
        '''
        path = getTestPath('rev0.db')
        with open(path, 'rb') as fd:
            byts = fd.read()

        # Hash of the rev0 file on initial commit prevent
        # commits which overwrite this accidentally from passing.
        known_hash = '50cae022b296e0c2b61fd6b101c4fdaf'
        self.eq(hashlib.md5(byts).hexdigest().lower(), known_hash)
        return byts

    def getRev0DbBytsMpk(self):
        '''
        Get the bytes for the rev0.db Cortex savefile.
        '''
        path = getTestPath('rev0.mpk')

        with open(path, 'rb') as fd:
            byts = fd.read()

        # Hash of the rev0 file on initial commit prevent
        # commits which overwrite this accidentally from passing.
        known_hash = '5f724ba09c719e1f83454431b516e429'
        self.eq(hashlib.md5(byts).hexdigest().lower(), known_hash)
        return byts

    def getRev0DbBytsLmdbGz(self):
        '''
        Get the bytes for the rev0.db Cortex in lmdb form.
        This bytestream has been gzip.compressed.
        '''
        path = getTestPath('rev0.lmdb.gz')
        with open(path, 'rb') as fd:
            byts = fd.read()

        # Hash of the rev0 file on initial commit prevent
        # commits which overwrite this accidentally from passing.
        known_hash = '06c05fa1ed9c9c195e060905357caef6'
        self.eq(hashlib.md5(byts).hexdigest().lower(), known_hash)
        return byts

    def getTestOutp(self):
        return TestOutPut()

    def thisHostMust(self, **props):
        for k, v in props.items():
            if s_thishost.get(k) != v:
                raise unittest.SkipTest('skip thishost: %s!=%r' % (k, v))

    def thisHostMustNot(self, **props):
        for k, v in props.items():
            if s_thishost.get(k) == v:
                raise unittest.SkipTest('skip thishost: %s==%r' % (k, v))

    def addTstForms(self, core):
        '''
        Add test forms to the cortex.

        Args:
            core (s_cores_common.Cortex): Core to prep.

        Returns:
            None
        '''
        # Some custom type machinations for later test use
        core.addTufoProp('inet:fqdn', 'inctest', ptype='int', defval=0)

        core.addTufoForm('strform', ptype='str')
        core.addTufoProp('strform', 'foo', ptype='str')
        core.addTufoProp('strform', 'bar', ptype='str')
        core.addTufoProp('strform', 'baz', ptype='int')

        core.addTufoForm('intform', ptype='int')
        core.addTufoProp('intform', 'foo', ptype='str')
        core.addTufoProp('intform', 'baz', ptype='int')

        core.addTufoForm('default_foo', ptype='str')
        core.addTufoProp('default_foo', 'p0', ptype='int')

        core.addTufoForm('guidform', ptype='guid')
        core.addTufoProp('guidform', 'foo', ptype='str')
        core.addTufoProp('guidform', 'baz', ptype='int')

    @contextlib.contextmanager
    def getRamCore(self):
        '''
        Context manager to make a ram:/// cortex which has test models
        loaded into it.

        Yields:
            s_cores_common.Cortex: Ram backed cortex with test models.
        '''
        with s_cortex.openurl('ram:///') as core:
            self.addTstForms(core)
            yield core

    @contextlib.contextmanager
    def getTestDir(self):
        tempdir = tempfile.mkdtemp()
        yield tempdir
        shutil.rmtree(tempdir, ignore_errors=True)

    def eq(self, x, y):
        self.assertEqual(x, y)

    def ne(self, x, y):
        self.assertNotEqual(x, y)

    def true(self, x):
        self.assertTrue(x)

    def false(self, x):
        self.assertFalse(x)

    def nn(self, x):
        self.assertIsNotNone(x)

    def none(self, x):
        self.assertIsNone(x)

    def noprop(self, info, prop):
        valu = info.get(prop, novalu)
        self.eq(valu, novalu)

    def raises(self, *args, **kwargs):
        return self.assertRaises(*args, **kwargs)

    def sorteq(self, x, y):
        return self.eq(sorted(x), sorted(y))

    def isinstance(self, obj, cls):
        self.assertIsInstance(obj, cls)

    def isin(self, member, container):
        self.assertIn(member, container)

    def notin(self, member, container):
        self.assertNotIn(member, container)

    def gt(self, x, y):
        self.assertGreater(x, y)

    def ge(self, x, y):
        self.assertGreaterEqual(x, y)

    def lt(self, x, y):
        self.assertLess(x, y)

    def le(self, x, y):
        self.assertLessEqual(x, y)


testdir = os.path.dirname(__file__)
def getTestPath(*paths):
    return os.path.join(testdir, *paths)

def getIngestCore(path, core=None):
    if core is None:
        core = s_cortex.openurl('ram:///')

    gest = s_ingest.loadfile(path)
    with core.getCoreXact() as xact:
        gest.ingest(core)

    return core
