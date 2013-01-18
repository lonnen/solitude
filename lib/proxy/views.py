import logging
import urlparse

from django import http
from django.conf import settings

from django_statsd.clients import statsd
from lxml import etree
import requests

from lib.bango.constants import HEADERS_SERVICE_GET

from lib.paypal.client import get_client as paypal_client
from lib.paypal.constants import HEADERS_URL_GET, HEADERS_TOKEN_GET
from lib.paypal.map import urls

log = logging.getLogger('s.proxy')
bango_timeout = getattr(settings, 'BANGO_TIMEOUT', 10)


class Proxy(object):
    # Override this in your proxy class.
    name = None
    # Values that we'll populate from the request, optionally.
    body = None
    headers = None
    url = None
    # Nice name for the URL we are going to hit.
    service = ''

    def __init__(self):
        pass

    def pre(self, request):
        """Do any processing of the incoming request."""
        self.body = request.raw_post_data
        try:
            self.service = request.META[HEADERS_URL_GET]
        except KeyError:
            log.error('Missing header: %s',
                      ', '.join(sorted(request.META.keys())))
            raise
        self.url = urls[self.service]

    def call(self):
        """Go all the proxied service, return a response."""
        response = http.HttpResponse()
        try:
            with statsd.timer('solitude.proxy.%s.%s' %
                              (self.service, self.name)):
                log.info('Calling service: %s' % self.service)
            # We aren't calling client._call because that tries to parse the
            # output. Once the headers are prepared, this will do the rest.
            result = requests.post(self.url, data=self.body,
                                   headers=self.headers,
                                   timeout=self.timeout, verify=True)
        except requests.exceptions.RequestException as err:
            log.error(err.__class__.__name__)
            response.status_code = 500
            return response

        response.status_code = result.status_code
        response.content = result.text
        return response

    def __call__(self, request):
        """Takes the incoming request and returns a response."""
        if not self.enabled:
            return http.HttpResponseNotFound()

        self.pre(request)
        response = self.call()
        response = self.post(response)
        return response

    def post(self, response):
        """Any post processing of the response here. Return the response."""
        return response


class PaypalProxy(Proxy):
    name = 'paypal'

    def __init__(self):
        self.enabled = getattr(settings, 'SOLITUDE_PROXY', False)
        self.timeout = getattr(settings, 'PAYPAL_TIMEOUT', 10)

    def pre(self, request):
        """
        Paypal does auth be special headers, so we'll need to process
        those and add those to the request.
        """
        super(PaypalProxy, self).pre(request)
        token = request.META.get(HEADERS_TOKEN_GET)
        if token:
            token = dict(urlparse.parse_qsl(token))

        client = paypal_client()
        self.headers = client.headers(self.url, auth_token=token)


class BangoProxy(Proxy):
    name = 'bango'
    namespaces = ['com.bango.webservices.billingconfiguration',
                  'com.bango.webservices.mozillaexporter']

    def __init__(self):
        self.enabled = getattr(settings, 'SOLITUDE_PROXY', False)
        self.timeout = getattr(settings, 'BANGO_TIMEOUT', 10)

    def tags(self, name):
        return ['{%s}%s' % (n, name) for n in self.namespaces]

    def pre(self, request):
        self.url = request.META[HEADERS_SERVICE_GET]
        self.headers = {'Content-Type': 'text/xml; charset=utf-8'}

        # Alter the XML to include the username and password from the config.
        # Perhaps this can be done quicker with XPath.
        root = etree.fromstring(request.raw_post_data)
        #log.info(request.raw_post_data)
        username = self.tags('username')
        password = self.tags('password')
        changed_username = False
        changed_password = False
        for element in root.iter():
            if element.tag in username:
                element.text = settings.BANGO_AUTH.get('USER', '')
                changed_username = True
            elif element.tag in password:
                element.text = settings.BANGO_AUTH.get('PASSWORD', '')
                changed_password = True
            if changed_username and changed_password:
                break

        if not changed_username and not changed_password:
            log.info('Did not set a username and password on the request.')

        self.body = etree.tostring(root)


def paypal(request):
    return PaypalProxy()(request)


def bango(request):
    return BangoProxy()(request)
