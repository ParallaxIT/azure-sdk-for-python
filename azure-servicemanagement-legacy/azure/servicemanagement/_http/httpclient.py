﻿#-------------------------------------------------------------------------
# Copyright (c) Microsoft.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#--------------------------------------------------------------------------
import base64
import os
import sys

if sys.version_info < (3,):
    from httplib import (
        HTTPSConnection,
        HTTPConnection,
        HTTP_PORT,
        HTTPS_PORT,
        )
    from urlparse import urlparse
    from urllib2 import quote as url_quote
else:
    from http.client import (
        HTTPSConnection,
        HTTPConnection,
        HTTP_PORT,
        HTTPS_PORT,
        )
    from urllib.parse import urlparse
    from urllib.parse import quote as url_quote

from . import HTTPError, HTTPResponse

DEBUG_REQUESTS = False
DEBUG_RESPONSES = False

class _HTTPClient(object):

    '''
    Takes the request and sends it to cloud service and returns the response.
    '''

    def __init__(self, service_instance, cert_file=None, protocol='https',
                 request_session=None, timeout=65, user_agent=''):
        '''
        service_instance:
            service client instance.
        cert_file:
            certificate file name/location. This is only used in hosted
            service management.
        protocol:
            http or https.
        request_session:
            session object created with requests library (or compatible).
        timeout:
            timeout for the http request, in seconds.
        user_agent:
            user agent string to set in http header.
        '''
        self.service_instance = service_instance
        self.cert_file = cert_file
        self.protocol = protocol
        self.proxy_host = None
        self.proxy_port = None
        self.proxy_user = None
        self.proxy_password = None
        self.request_session = request_session
        self.timeout = timeout
        self.user_agent = user_agent
        if request_session:
            self.use_httplib = True
        else:
            self.use_httplib = self.should_use_httplib()

    def should_use_httplib(self):
        if sys.platform.lower().startswith('win') and self.cert_file:
            # On Windows, auto-detect between Windows Store Certificate
            # (winhttp) and OpenSSL .pem certificate file (httplib).
            #
            # We used to only support certificates installed in the Windows
            # Certificate Store.
            #   cert_file example: CURRENT_USER\my\CertificateName
            #
            # We now support using an OpenSSL .pem certificate file,
            # for a consistent experience across all platforms.
            #   cert_file example: account\certificate.pem
            #
            # When using OpenSSL .pem certificate file on Windows, make sure
            # you are on CPython 2.7.4 or later.

            # If it's not an existing file on disk, then treat it as a path in
            # the Windows Certificate Store, which means we can't use httplib.
            if not os.path.isfile(self.cert_file):
                return False

        return True

    def set_proxy(self, host, port, user, password):
        '''
        Sets the proxy server host and port for the HTTP CONNECT Tunnelling.

        host:
            Address of the proxy. Ex: '192.168.0.100'
        port:
            Port of the proxy. Ex: 6000
        user:
            User for proxy authorization.
        password:
            Password for proxy authorization.
        '''
        self.proxy_host = host
        self.proxy_port = port
        self.proxy_user = user
        self.proxy_password = password

    def get_uri(self, request):
        ''' Return the target uri for the request.'''
        protocol = request.protocol_override \
            if request.protocol_override else self.protocol
        protocol = protocol.lower()
        port = HTTP_PORT if protocol == 'http' else HTTPS_PORT
        return protocol + '://' + request.host + ':' + str(port) + request.path

    def get_connection(self, request):
        ''' Create connection for the request. '''
        protocol = request.protocol_override \
            if request.protocol_override else self.protocol
        protocol = protocol.lower()
        target_host = request.host
        target_port = HTTP_PORT if protocol == 'http' else HTTPS_PORT

        if self.request_session:
            from .requestsclient import _RequestsConnection
            connection = _RequestsConnection(
                target_host, protocol, self.request_session, self.timeout)
            proxy_host = self.proxy_host
            proxy_port = self.proxy_port
        elif not self.use_httplib:
            from .winhttp import _HTTPConnection
            connection = _HTTPConnection(
                target_host, self.cert_file, protocol, self.timeout)
            proxy_host = self.proxy_host
            proxy_port = self.proxy_port
        else:
            if ':' in target_host:
                target_host, _, target_port = target_host.rpartition(':')
            if self.proxy_host:
                proxy_host = target_host
                proxy_port = target_port
                host = self.proxy_host
                port = self.proxy_port
            else:
                host = target_host
                port = target_port

            if protocol.lower() == 'http':
                connection = HTTPConnection(host, int(port),
                                            timeout=self.timeout)
            else:
                connection = HTTPSConnection(
                    host, int(port), cert_file=self.cert_file,
                    timeout=self.timeout)

        if self.proxy_host:
            headers = None
            if self.proxy_user and self.proxy_password:
                auth = base64.encodestring(
                    "{0}:{1}".format(self.proxy_user, self.proxy_password))
                headers = {'Proxy-Authorization': 'Basic {0}'.format(auth)}
            connection.set_tunnel(proxy_host, int(proxy_port), headers)

        return connection

    def send_request_headers(self, connection, request_headers):
        if self.use_httplib:
            if self.proxy_host and self.request_session is None:
                for i in connection._buffer:
                    if i.startswith(b"Host: "):
                        connection._buffer.remove(i)
                connection.putheader(
                    'Host', "{0}:{1}".format(connection._tunnel_host,
                                             connection._tunnel_port))

        for name, value in request_headers:
            if value:
                connection.putheader(name, value)

        connection.putheader('User-Agent', self.user_agent)
        connection.endheaders()

    def send_request_body(self, connection, request_body):
        if request_body:
            assert isinstance(request_body, bytes)
            connection.send(request_body)
        elif (not isinstance(connection, HTTPSConnection) and
              not isinstance(connection, HTTPConnection)):
            connection.send(None)

    def _update_request_uri_query(self, request):
        '''pulls the query string out of the URI and moves it into
        the query portion of the request object.  If there are already
        query parameters on the request the parameters in the URI will
        appear after the existing parameters'''

        if '?' in request.path:
            request.path, _, query_string = request.path.partition('?')
            if query_string:
                query_params = query_string.split('&')
                for query in query_params:
                    if '=' in query:
                        name, _, value = query.partition('=')
                        request.query.append((name, value))

        request.path = url_quote(request.path, '/()$=\',')

        # add encoded queries to request.path.
        if request.query:
            request.path += '?'
            for name, value in request.query:
                if value is not None:
                    request.path += name + '=' + url_quote(value, '/()$=\',') + '&'
            request.path = request.path[:-1]

        return request.path, request.query

    def perform_request(self, request):
        ''' Sends request to cloud service server and return the response. '''
        connection = self.get_connection(request)
        try:
            connection.putrequest(request.method, request.path)

            if not self.use_httplib:
                if self.proxy_host and self.proxy_user:
                    connection.set_proxy_credentials(
                        self.proxy_user, self.proxy_password)

            self.send_request_headers(connection, request.headers)
            self.send_request_body(connection, request.body)

            if DEBUG_REQUESTS and request.body:
                print('request:')
                try:
                    print(request.body)
                except:
                    pass

            resp = connection.getresponse()
            status = int(resp.status)
            message = resp.reason
            respheaders = resp.getheaders()

            # for consistency across platforms, make header names lowercase
            for i, value in enumerate(respheaders):
                respheaders[i] = (value[0].lower(), value[1])

            respbody = None
            if resp.length is None:
                respbody = resp.read()
            elif resp.length > 0:
                respbody = resp.read(resp.length)

            if DEBUG_RESPONSES and respbody:
                print('response:')
                try:
                    print(respbody)
                except:
                    pass

            response = HTTPResponse(
                status, resp.reason, respheaders, respbody)
            if status == 307:
                new_url = urlparse(dict(respheaders)['location'])
                request.host = new_url.hostname
                request.path = new_url.path
                request.path, request.query = self._update_request_uri_query(request)
                return self.perform_request(request)
            if status >= 300:
                raise HTTPError(status, message, respheaders, respbody)

            return response
        finally:
            connection.close()
