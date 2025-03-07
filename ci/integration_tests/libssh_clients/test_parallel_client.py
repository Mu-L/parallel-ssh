# This file is part of parallel-ssh.
#
# Copyright (C) 2014-2022 Panos Kittenis and contributors.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation, version 2.1.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

import os
import unittest
from datetime import datetime
from sys import version_info
from unittest.mock import patch, MagicMock

from gevent import joinall, spawn, socket, sleep

from pssh import logger as pssh_logger
from pssh.clients.ssh.parallel import ParallelSSHClient
from pssh.exceptions import AuthenticationException, ConnectionErrorException, Timeout, PKeyFileError
from pssh.output import HostOutput
from .base_ssh_case import PKEY_FILENAME, PUB_FILE, USER_CERT_PRIV_KEY, \
    USER_CERT_FILE, CA_USER_KEY, USER, sign_cert
from ..embedded_server.openssh import OpenSSHServer


class LibSSHParallelTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _mask = int('0600') if version_info <= (2,) else 0o600
        for _file in [PKEY_FILENAME, USER_CERT_PRIV_KEY, CA_USER_KEY]:
            os.chmod(_file, _mask)
        sign_cert()
        cls.host = '127.0.0.1'
        cls.port = 2422
        cls.server = OpenSSHServer(listen_ip=cls.host, port=cls.port)
        cls.server.start_server()
        cls.cmd = 'echo me'
        cls.resp = u'me'
        cls.user_key = PKEY_FILENAME
        cls.user_pub_key = PUB_FILE
        cls.cert_pkey = USER_CERT_PRIV_KEY
        cls.cert_file = USER_CERT_FILE
        cls.user = USER
        # Single client for all tests ensures that the client does not do
        # anything that causes server to disconnect the session and
        # affect all subsequent uses of the same session.
        cls.client = ParallelSSHClient([cls.host],
                                       pkey=PKEY_FILENAME,
                                       port=cls.port,
                                       num_retries=1,
                                       retry_delay=.1,
                                       )

    @classmethod
    def tearDownClass(cls):
        del cls.client
        cls.server.stop()
        del cls.server

    def setUp(self):
        self.long_cmd = lambda lines: 'for (( i=0; i<%s; i+=1 )) do echo $i; sleep .1; done' % (lines,)

    def make_random_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', 0))
        listen_port = sock.getsockname()[1]
        sock.close()
        return listen_port

    def test_timeout_on_open_session(self):
        timeout = .1
        client = ParallelSSHClient([self.host], port=self.port,
                                   pkey=self.user_key,
                                   timeout=timeout,
                                   num_retries=1)

        def _session(_=None):
            sleep(.2)
        joinall(client.connect_auth())
        sleep(.01)
        client._host_clients[(0, self.host)].open_session = _session
        self.assertRaises(Timeout, client.run_command, self.cmd)

    def test_pkey_from_memory(self):
        with open(self.user_key, 'rb') as fh:
            key = fh.read()
        client = ParallelSSHClient([self.host], pkey=key, port=self.port, num_retries=1)
        joinall(client.connect_auth(), raise_error=True)

    def test_join_timeout(self):
        client = ParallelSSHClient([self.host], port=self.port,
                                   pkey=self.user_key)
        output = client.run_command('echo me; sleep .5')
        self.assertRaises(Timeout, client.join, output, timeout=.1)
        self.assertFalse(output[0].client.finished(output[0].channel))
        self.assertFalse(output[0].channel.is_eof())
        client.join(output, timeout=1)
        self.assertTrue(output[0].channel.is_eof())
        self.assertTrue(client.finished(output))

    def test_client_join_stdout(self):
        output = self.client.run_command(self.cmd)
        expected_exit_code = 0
        expected_stdout = [self.resp]
        expected_stderr = []
        stdout = list(output[0].stdout)
        stderr = list(output[0].stderr)
        self.assertEqual(expected_stdout, stdout,
                         msg="Got unexpected stdout - %s, expected %s" %
                         (stdout, expected_stdout,))
        self.assertEqual(expected_stderr, stderr,
                         msg="Got unexpected stderr - %s, expected %s" %
                         (stderr, expected_stderr,))
        self.client.join(output)
        exit_code = output[0].exit_code
        self.assertEqual(expected_exit_code, exit_code,
                         msg="Got unexpected exit code - %s, expected %s" %
                         (exit_code, expected_exit_code,))
        output = self.client.run_command(";".join([self.cmd, 'exit 1']))
        self.client.join(output)
        exit_code = output[0].exit_code
        self.assertEqual(exit_code, 1)
        self.assertTrue(len(output), len(self.client.cmds))
        _output = self.client.get_last_output()
        self.assertEqual(len(_output), len(output))
        for i, host in enumerate(self.client.hosts):
            self.assertEqual(_output[i].host, host)

    def test_get_last_output(self):
        host = '127.0.0.9'
        server = OpenSSHServer(listen_ip=host, port=self.port)
        server.start_server()
        try:
            hosts = [self.host, host]
            client = ParallelSSHClient(hosts, port=self.port, pkey=self.user_key)
            self.assertTrue(client.cmds is None)
            self.assertTrue(client.get_last_output() is None)
            client.run_command(self.cmd)
            self.assertTrue(client.cmds is not None)
            self.assertEqual(len(client.cmds), len(hosts))
            expected_stdout = [self.resp]
            expected_stderr = []
            output = client.get_last_output()
            self.assertIsInstance(output, list)
            self.assertEqual(len(output), len(hosts))
            self.assertIsInstance(output[0], HostOutput)
            client.join(output)
            for i, host in enumerate(hosts):
                self.assertEqual(output[i].host, host)
                exit_code = output[i].exit_code
                _stdout = list(output[i].stdout)
                _stderr = list(output[i].stderr)
                self.assertEqual(exit_code, 0)
                self.assertListEqual(expected_stdout, _stdout)
                self.assertListEqual(expected_stderr, _stderr)
        finally:
            server.stop()

    def test_pssh_client_no_stdout_non_zero_exit_code_immediate_exit(self):
        output = self.client.run_command('exit 1')
        expected_exit_code = 1
        self.client.join(output)
        exit_code = output[0].exit_code
        self.assertEqual(expected_exit_code, exit_code,
                         msg="Got unexpected exit code - %s, expected %s" %
                         (exit_code,
                          expected_exit_code,))

    def test_pssh_client_run_command_get_output(self):
        output = self.client.run_command(self.cmd)
        expected_exit_code = 0
        expected_stdout = [self.resp]
        expected_stderr = []
        stdout = list(output[0].stdout)
        stderr = list(output[0].stderr)
        exit_code = output[0].exit_code
        self.assertEqual(expected_exit_code, exit_code,
                         msg="Got unexpected exit code - %s, expected %s" %
                         (exit_code,
                          expected_exit_code,))
        self.assertEqual(expected_stdout, stdout,
                         msg="Got unexpected stdout - %s, expected %s" %
                         (stdout,
                          expected_stdout,))
        self.assertEqual(expected_stderr, stderr,
                         msg="Got unexpected stderr - %s, expected %s" %
                         (stderr,
                          expected_stderr,))
    #
    # def test_pssh_client_run_long_command(self):
    #     expected_lines = 5
    #     output = self.client.run_command(self.long_cmd(expected_lines))
    #     self.assertEqual(len(output), len(self.client.hosts))
    #     stdout = list(output[0].stdout)
    #     self.client.join(output)
    #     self.assertTrue(len(stdout) == expected_lines,
    #                     msg="Expected %s lines of response, got %s" % (
    #                         expected_lines, len(stdout)))

    def test_pssh_client_auth_failure(self):
        client = ParallelSSHClient([self.host], port=self.port,
                                   user='FAKE USER',
                                   pkey=self.user_key,
                                   num_retries=1)
        self.assertRaises(
            AuthenticationException, client.run_command, self.cmd)

    def test_pssh_client_hosts_list_part_failure(self):
        """Test getting output for remainder of host list in the case where one
        host in the host list has a failure"""
        hosts = [self.host, '127.1.1.100']
        client = ParallelSSHClient(hosts,
                                   port=self.port,
                                   pkey=self.user_key,
                                   num_retries=1)
        output = client.run_command(self.cmd, stop_on_errors=False)
        self.assertFalse(client.finished(output))
        client.join(output, consume_output=True)
        self.assertTrue(client.finished(output))
        self.assertEqual(output[0].host, hosts[0],
                         msg="Successful host does not exist in output - output is %s" % (output,))
        self.assertEqual(output[1].host, hosts[1],
                         msg="Failed host does not exist in output - output is %s" % (output,))
        self.assertIsNotNone(output[1].exception,
                             msg="Failed host %s has no exception in output - %s" % (hosts[1], output,))
        self.assertTrue(output[1].exception is not None)
        self.assertEqual(output[1].host, hosts[1])
        self.assertEqual(output[1].exception.args[-2], hosts[1])
        try:
            raise output[1].exception
        except ConnectionErrorException:
            pass
        else:
            raise Exception("Expected ConnectionError, got %s instead" % (output[1].exception,))

    def test_pssh_client_timeout(self):
        # 1ms timeout
        client_timeout = 0.00001
        client = ParallelSSHClient([self.host], port=self.port,
                                   pkey=self.user_key,
                                   timeout=client_timeout,
                                   num_retries=1)
        now = datetime.now()
        output = client.run_command('sleep 1', stop_on_errors=False)
        dt = datetime.now() - now
        pssh_logger.debug("Run command took %s", dt)
        self.assertIsInstance(output[0].exception,
                              Timeout)

    def test_connection_timeout(self):
        client_timeout = .01
        host = 'fakehost.com'
        client = ParallelSSHClient([host], port=self.port,
                                   pkey=self.user_key,
                                   timeout=client_timeout,
                                   num_retries=1,
                                   retry_delay=.1)
        output = client.run_command('sleep 1', stop_on_errors=False)
        self.assertIsInstance(output[0].exception, ConnectionErrorException)

    def test_zero_timeout(self):
        host = '127.0.0.2'
        server = OpenSSHServer(listen_ip=host, port=self.port)
        server.start_server()
        client = ParallelSSHClient([self.host, host],
                                   port=self.port,
                                   pkey=self.user_key,
                                   timeout=0)
        cmd = spawn(client.run_command, 'sleep 1', stop_on_errors=False)
        output = cmd.get(timeout=3)
        self.assertTrue(output[0].exception is None)
    #
    # def test_pssh_client_long_running_command_exit_codes(self):
    #     expected_lines = 2
    #     output = self.client.run_command(self.long_cmd(expected_lines))
    #     self.assertEqual(len(output), len(self.client.hosts))
    #     self.assertIsNone(output[0].exit_code)
    #     self.assertFalse(self.client.finished(output))
    #     self.client.join(output, consume_output=True)
    #     self.assertTrue(self.client.finished(output))
    #     self.assertEqual(output[0].exit_code, 0)
    #
    # def test_pssh_client_long_running_command_exit_codes_no_stdout(self):
    #     expected_lines = 2
    #     output = self.client.run_command(self.long_cmd(expected_lines))
    #     self.assertEqual(len(output), len(self.client.hosts))
    #     self.assertIsNone(output[0].exit_code)
    #     self.assertFalse(self.client.finished(output))
    #     self.client.join(output)
    #     self.assertTrue(self.client.finished(output))
    #     self.assertEqual(output[0].exit_code, 0)
    #     stdout = list(output[0].stdout)
    #     self.assertEqual(expected_lines, len(stdout))

    def test_connection_error_exception(self):
        """Test that we get connection error exception in output with correct arguments"""
        # Make port with no server listening on it on separate ip
        host = '127.0.0.3'
        port = self.make_random_port()
        hosts = [host]
        client = ParallelSSHClient(hosts, port=port,
                                   pkey=self.user_key,
                                   num_retries=1)
        output = client.run_command(self.cmd, stop_on_errors=False)
        client.join(output)
        self.assertIsInstance(output[0].exception, ConnectionErrorException)
        self.assertEqual(output[0].host, host)
        try:
            raise output[0].exception
        except ConnectionErrorException as ex:
            self.assertEqual(ex.args[-2], host)
            self.assertEqual(ex.args[-1], port)
        else:
            raise Exception("Expected ConnectionErrorException")

    def test_bad_pkey_path(self):
        self.assertRaises(PKeyFileError, ParallelSSHClient, [self.host], port=self.port,
                          pkey='A REALLY FAKE KEY',
                          num_retries=1)

    def test_multiple_single_quotes_in_cmd(self):
        """Test that we can run a command with multiple single quotes"""
        output = self.client.run_command("echo 'me' 'and me'")
        stdout = list(output[0].stdout)
        expected = 'me and me'
        self.assertTrue(len(stdout) == 1,
                        msg="Got incorrect number of lines in output - %s" % (stdout,))
        self.assertEqual(output[0].exit_code, 0)
        self.assertEqual(expected, stdout[0],
                         msg="Got unexpected output. Expected %s, got %s" % (
                             expected, stdout[0],))

    def test_backtics_in_cmd(self):
        """Test running command with backtics in it"""
        output = self.client.run_command("out=`ls` && echo $out")
        self.client.join(output)
        self.assertEqual(output[0].exit_code, 0)

    def test_multiple_shell_commands(self):
        """Test running multiple shell commands in one go"""
        output = self.client.run_command("echo me; echo and; echo me")
        stdout = list(output[0].stdout)
        expected = ["me", "and", "me"]
        self.assertEqual(output[0].exit_code, 0)
        self.assertEqual(expected, stdout,
                         msg="Got unexpected output. Expected %s, got %s" % (
                             expected, stdout,))

    def test_escaped_quotes(self):
        """Test escaped quotes in shell variable are handled correctly"""
        output = self.client.run_command('t="--flags=\\"this\\""; echo $t')
        stdout = list(output[0].stdout)
        expected = ['--flags="this"']
        self.assertEqual(output[0].exit_code, 0)
        self.assertEqual(expected, stdout,
                         msg="Got unexpected output. Expected %s, got %s" % (
                             expected, stdout,))

    def test_read_timeout(self):
        client = ParallelSSHClient([self.host], port=self.port,
                                   pkey=self.user_key)
        output = client.run_command('sleep .3; echo me; echo me; echo me', read_timeout=.2)
        for host_out in output:
            self.assertRaises(Timeout, list, host_out.stdout)
        self.assertFalse(output[0].channel.is_eof())
        client.join(output)
        for host_out in output:
            stdout = list(host_out.stdout)
            self.assertEqual(len(stdout), 3)
        self.assertTrue(output[0].channel.is_eof())

    def test_timeout_file_read(self):
        dir_name = os.path.dirname(__file__)
        _file = os.sep.join((dir_name, 'file_to_read'))
        contents = [b'a line\n' for _ in range(50)]
        with open(_file, 'wb') as fh:
            fh.writelines(contents)
        try:
            output = self.client.run_command(
                'tail -f %s' % (_file,), use_pty=True, read_timeout=.1)
            self.assertRaises(Timeout, self.client.join, output, timeout=.1)
            for host_out in output:
                try:
                    for line in host_out.stdout:
                        pass
                except Timeout:
                    pass
                else:
                    raise Exception("Timeout should have been raised")
            self.assertRaises(Timeout, self.client.join, output, timeout=.1)
            channel = output[0].channel
            output[0].client.close_channel(channel)
            self.client.join(output)
        finally:
            os.unlink(_file)

    def test_file_read_no_timeout(self):
        dir_name = os.path.dirname(__file__)
        _file = os.sep.join((dir_name, 'file_to_read'))
        contents = [b'a line\n' for _ in range(1000)]
        with open(_file, 'wb') as fh:
            fh.writelines(contents)
        try:
            output = self.client.run_command('cat %s' % (_file,), read_timeout=10)
            _out = list(output[0].stdout)
        finally:
            os.unlink(_file)
        _contents = [c.decode('utf-8').strip() for c in contents]
        self.assertEqual(len(contents), len(_out))
        self.assertListEqual(_contents, _out)

    def test_gssapi_auth(self):
        _server_id = 'server_id'
        _client_id = 'client_id'
        client = ParallelSSHClient(
            [self.host], port=self.port, num_retries=1,
            pkey=None,
            gssapi_server_identity=_server_id,
            gssapi_client_identity=_client_id,
            gssapi_delegate_credentials=True,
            identity_auth=False)
        self.assertRaises(AuthenticationException, client.run_command, self.cmd)
        client = ParallelSSHClient(
            [self.host], port=self.port, num_retries=1,
            pkey=None,
            gssapi_auth=True,
            identity_auth=False)
        self.assertRaises(AuthenticationException, client.run_command, self.cmd)

    def test_long_running_cmd_join_timeout(self):
        output = self.client.run_command('sleep 1')
        self.assertRaises(Timeout, self.client.join, output, timeout=0.2)

    def test_default_finished(self):
        client = ParallelSSHClient([self.host], port=self.port,
                                   pkey=self.user_key)
        self.assertTrue(client.finished())

    def test_agent_auth(self):
        client = ParallelSSHClient(
            [self.host], port=self.port,
            num_retries=1,
            pkey=None, allow_agent=True,
            identity_auth=True)
        self.assertRaises(AuthenticationException, client.run_command, self.cmd)

    def test_multiple_join_timeout(self):
        client = ParallelSSHClient([self.host], port=self.port,
                                   pkey=self.user_key)
        for _ in range(5):
            output = client.run_command(self.cmd)
            client.join(output, timeout=1, consume_output=True)
            for host_out in output:
                self.assertTrue(host_out.client.finished(host_out.channel))
        output = client.run_command('sleep .2')
        self.assertRaises(Timeout, client.join, output, timeout=.1, consume_output=True)
        for host_out in output:
            self.assertFalse(host_out.client.finished(host_out.channel))

    def test_cert_auth(self):
        client = ParallelSSHClient([self.host], port=self.port,
                                   pkey=self.cert_pkey,
                                   cert_file=self.cert_file)
        output = client.run_command(self.cmd)
        client.join(output)
        resp = list(output[0].stdout)
        self.assertListEqual(resp, [self.resp])

    def test_read_multi_same_hosts(self):
        hosts = [self.host, self.host]
        outputs = [
            self.client.run_command(self.cmd),
            self.client.run_command(self.cmd),
        ]
        for output in outputs:
            for i, host_out in enumerate(output):
                stdout = list(host_out.stdout)
                self.assertEqual(host_out.client.host, hosts[i])
                self.assertListEqual(stdout, [self.resp])

    def test_join_bad_host_out(self):
        out = HostOutput(None, None, None, None)
        self.assertIsNone(self.client._join(out))
        self.assertIsNone(self.client._join(None))
        self.assertIsNone(self.client.join([None]))

    @patch('pssh.clients.base.single.socket')
    def test_ipv6(self, gsocket):
        hosts = ['::1']
        client = ParallelSSHClient(hosts, port=self.port, pkey=self.user_key, num_retries=1)
        addr_info = ('::1', self.port, 0, 0)
        gsocket.IPPROTO_TCP = socket.IPPROTO_TCP
        gsocket.socket = MagicMock()
        _sock = MagicMock()
        gsocket.socket.return_value = _sock
        sock_con = MagicMock()
        _sock.connect = sock_con
        getaddrinfo = MagicMock()
        gsocket.getaddrinfo = getaddrinfo
        getaddrinfo.return_value = [(
            socket.AF_INET6, socket.SocketKind.SOCK_STREAM, socket.IPPROTO_TCP, '', addr_info)]
        output = client.run_command(self.cmd, stop_on_errors=False)
        for host_out in output:
            self.assertEqual(hosts[0], host_out.host)
            self.assertIsInstance(host_out.exception, TypeError)

    # def test_multiple_run_command_timeout(self):
    #     client = ParallelSSHClient([self.host], port=self.port,
    #                                pkey=self.user_key)
    #     for _ in range(5):
    #         output = client.run_command('pwd', return_list=True, timeout=1)
    #         for host_out in output:
    #             stdout = list(host_out.stdout)
    #             self.assertTrue(len(stdout) > 0)
    #             self.assertTrue(host_out.client.finished(host_out.channel))
    #     output = client.run_command('sleep 2; echo me', return_list=True, timeout=1)
    #     for host_out in output:
    #         self.assertRaises(Timeout, list, host_out.stdout)
    #     client.join(output)
    #     for host_out in output:
    #         stdout = list(host_out.stdout)
    #         self.assertEqual(stdout, ['me'])

    # def test_client_scope(self):
    #     def scope_killer():
    #         for _ in range(5):
    #             client = ParallelSSHClient([self.host], port=self.port,
    #                                        pkey=self.user_key, num_retries=1,
    #                                        timeout=1)
    #             output = client.run_command(self.cmd)
    #             client.join(output)
    #     scope_killer()
