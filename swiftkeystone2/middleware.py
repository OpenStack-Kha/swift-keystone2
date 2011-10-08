import json

from urllib import quote
from urlparse import urlparse

from webob.exc import HTTPForbidden, HTTPNotFound, \
    HTTPUnauthorized

from swift.common.utils import get_logger, split_path
from swift.common.middleware.acl import clean_acl, parse_acl, referrer_allowed
from swift.common.bufferedhttp import http_connect_raw as http_connect


class KeystoneAuth(object):
    def __init__(self, app, conf):
        self.app = app
        self.conf = conf
        self.logger = get_logger(conf, log_route='keystone')
        self.reseller_prefix = conf.get('reseller_prefix', 'AUTH').strip()
        #TODO: Error out if no url
        self.keystone_url = urlparse(conf.get('keystone_url'))
        self.admin_token = conf.get('keystone_admin_token')

    def __call__(self, environ, start_response):
        self.logger.debug('Starting middleware ')

        token = environ.get('HTTP_X_AUTH_TOKEN',
                            environ.get('HTTP_X_STORAGE_TOKEN'))

        if not token:
            environ['swift.authorize'] = self.denied_response
            return self.app(environ, start_response)

        self.logger.debug('token %s ' % (token))
        identity = self._keystone_validate_token(token)

        if not identity:
            #TODO: non authenticated access allow via refer
            environ['swift.authorize'] = self.denied_response
            return self.app(environ, start_response)

        self.logger.debug("identity: %r" % (identity))
        environ['keystone.identity'] = identity
        environ['REMOTE_USER'] = identity.get('tenant')
        environ['swift.authorize'] = self.authorize
        environ['swift.clean_acl'] = clean_acl
        return self.app(environ, start_response)

    def _keystone_validate_token(self, claim):
        headers = {"X-Auth-Token": self.admin_token}
        conn = http_connect(self.keystone_url.hostname,
                            self.keystone_url.port, 'GET',
                            '%s/tokens/%s' % \
                                (self.keystone_url.path,
                                 quote(claim)),
                            headers=headers,
                            ssl=(self.keystone_url.scheme == 'https'))
        resp = conn.getresponse()
        data = resp.read()
        conn.close()

        if not str(resp.status).startswith('20'):
            #TODO: Make the self.keystone_url more meaningfull
            self.logger.debug('Error: Keystone : %s Returned: %d' % \
                                  (self.keystone_url, resp.status))
            return False
        identity_info = json.loads(data)

        try:
            tenant = identity_info['access']['token']['tenant']['id']
            user = identity_info['access']['user']['username']
            roles = [x['name'] for x in \
                         identity_info['access']['user']['roles']]
        except(KeyError, IndexError):
            tenant = None
            user = None
            roles = []

        identity = {'user': user,
                    'tenant': tenant,
                    'roles': roles}
        return identity

    def authorize(self, req):
        env = req.environ
        env_identity = env.get('keystone.identity', {})
        tenant = env_identity.get('tenant')

        try:
            version, account, container, obj = split_path(req.path, 1, 4, True)
        except ValueError:
            return HTTPNotFound(request=req)

        if account != '%s_%s' % (self.reseller_prefix, tenant):
            self.log.debug('tenant mismatch')
            return self.denied_response(req)

        user_groups = env_identity.get('roles', [])
        #TODO: setting?
        if 'Admin' in user_groups:
            req.environ['swift_owner'] = True
            return None

        # Check if Referrer allow it #TODO: check if it works
        referrers, groups = parse_acl(getattr(req, 'acl', None))
        if referrer_allowed(req.referer, referrers):
            if obj or '.rlistings' in groups:
                self.logger.debug('authorizing via ACL')
                return None
            return self.denied_response(req)

        # Check if we have the group in the group user and allow it
        for user_group in user_groups:
            if user_group in groups:
                return None

        return self.denied_response(req)

    def denied_response(self, req):
        """
        Returns a standard WSGI response callable with the status of 403 or 401
        depending on whether the REMOTE_USER is set or not.
        """
        if req.remote_user:
            return HTTPForbidden(request=req)
        else:
            return HTTPUnauthorized(request=req)


def filter_factory(global_conf, **local_conf):
    """Returns a WSGI filter app for use with paste.deploy."""
    conf = global_conf.copy()
    conf.update(local_conf)

    def auth_filter(app):
        return KeystoneAuth(app, conf)
    return auth_filter
