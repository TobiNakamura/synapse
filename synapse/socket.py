import os
import queue
import socket
import msgpack
import selectors
import threading
import traceback

import synapse.common as s_common
import synapse.threads as s_threads

from synapse.eventbus import EventBus
from synapse.statemach import keepstate

class Socket(EventBus):

    def __init__(self, sock, **info):
        EventBus.__init__(self)
        self.sock = sock
        self.unpk = msgpack.Unpacker(use_list=0,encoding='utf8')
        self.ident = s_common.guid()
        self.crypto = None
        self.sockinfo = info

        self.synOnFini(self._finiSocket)

    def getSockId(self):
        '''
        Get the GUID for this socket.

        Examples:

            sid = sock.getSockId()

        '''
        return self.ident

    def getSockInfo(self, prop):
        '''
        Retrieve a property from the socket's info dict.

        Example:

            if sock.getSockInfo('listen'):
                dostuff()

        '''
        return self.sockinfo.get(prop)

    def setSockInfo(self, prop, valu):
        '''
        Set a property on the Socket by name.

        Example:

            sock.setSockInfo('woot', 30)

        '''
        self.sockinfo[prop] = valu

    def recvall(self, size):
        '''
        Recieve the exact number of bytes requested.
        Returns None on if socket closes early.

        Example:

            byts = sock.recvall(300)
            if byts == None:
                return

            dostuff(byts)

        Notes:
            * this API will trigger synFini() on close

        '''
        byts = b''
        remain = size
        while remain:
            x = self.recv(remain)
            if not x:
                return None
            byts += x
            remain -= len(x)

        if self.crypto:
            byts = self.crypto.decrypt(byts)

        return byts

    def sendall(self, byts):
        if self.crypto:
            byts = self.crypto.encrypt(byts)
        return self.sock.sendall(byts)

    def fireobj(self, name, **info):
        return self.sendobj( (name,info) )

    def sendobj(self, msg):
        '''
        Serialize an object using msgpack and send on the socket.
        Returns True on success and False on socket error.

        Example:

            tufo = ('woot',{'foo':'bar'})
            sock.sendobj(tufo)

        Notes:

            * This method will trigger synFini() on errors.

        '''
        try:
            self.sendall( msgpack.dumps(msg, use_bin_type=True) )
            return True
        except socket.error as e:
            self.close()
            return False

    def senderr(self, code, msg, **info):
        info['msg'] = msg
        info['code'] = code
        return self.sendobj( ('err',info) )

    def recvobj(self):
        '''
        Recieve one msgpack'd socket message.
        '''
        while not self.isfini:
            byts = self.recv(102400)
            if not byts:
                return None

            try:
                self.unpk.feed(byts)
                for obj in self.unpk:
                    return obj

            except Exception as e:
                self.close()

    def __iter__(self):
        '''
        Receive loop which yields messages until socket close.
        '''
        while not self.isfini:

            byts = self.recv(1024000)
            if not byts:
                self.close()
                return

            try:
                self.unpk.feed(byts)
                for obj in self.unpk:
                    yield obj

            except Exception as e:
                self.close()

    def accept(self):
        conn,addr = self.sock.accept()
        return Socket(conn),addr

    def close(self):
        '''
        Hook the socket close() function to trigger synFini()
        '''
        self.synFini()

    def recv(self, size):
        '''
        Slighly modified recv function which masks socket errors.
        ( makes them look like a simple close )
        '''
        try:
            byts = self.sock.recv(size)
            if self.crypto:
                byts = self.crypto.decrypt(byts)

            if not byts:
                self.close()

            return byts
        except socket.error as e:
            # synFini triggered above.
            return b''

    def _setCryptoProv(self, prov):
        prov.initSockCrypto(self)
        self.crypto = prov

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def _finiSocket(self):
        try:
            self.sock.close()
        except OSError as e:
            pass

def listen(sockaddr):
    '''
    Simplified listening socket contructor.
    '''
    sock = socket.socket()
    try:
        sock.bind(sockaddr)
        sock.listen(120)
        return Socket(sock,listen=True)
    except socket.error as e:
        sock.close()
    return None

def connect(sockaddr):
    '''
    Simplified connected TCP socket constructor.
    '''
    sock = socket.socket()
    try:
        sock.connect(sockaddr)
        return Socket(sock)
    except socket.error as e:
        sock.close()
    return None

def _sockpair():
    s = socket.socket()
    s.bind(('127.0.0.1',0))
    s.listen(1)

    s1 = socket.socket()
    s1.connect( s.getsockname() )

    s2 = s.accept()[0]

    s.close()
    return Socket(s1),Socket(s2)

def socketpair():
    '''
    Standard sockepair() on posix systems, and pure shinanegans on windows.
    '''
    try:
        s1,s2 = socket.socketpair()
        return Socket(s1),Socket(s2)
    except AttributeError as e:
        return _sockpair()
