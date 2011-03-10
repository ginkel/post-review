#!/usr/bin/env python

'''
Post Review - A Review Board Client
'''

import cookielib
import difflib
import getpass
import mimetools
import ntpath
import os
import re
import shutil
import json as simplejson
import socket
import subprocess
import sys
import tempfile
import urllib
import urllib2
import httplib
import base64
import string
import xml.sax
import xml.sax.handler
import datetime
from optparse import OptionParser
from tempfile import mkstemp
from urlparse import urljoin, urlparse
from scm.dtr import DtrBaseClient, DtrVersion, DtrFile, DtrCollection
from gui.dialogs import AboutBox, ReviewPostedDialog, UpdateAvailableDialog, LoginDialog, PerforceUnavailableDialog
from gui.preferences import EditPreferences, get_scm_user, get_dtr_server
import wx
import wx.lib.newevent
import threading
import traceback
import getpass
import constants

try:
    from hashlib import md5
except ImportError:
    # Support Python versions before 2.5.
    from md5 import md5

# This specific import is necessary to handle the paths for
# cygwin enabled machines.
if (sys.platform.startswith('win')
    or sys.platform.startswith('cygwin')):
    import ntpath as cpath
else:
    import posixpath as cpath

###
# Default configuration -- user-settable variables follow.
###

# The following settings usually aren't needed, but if your Review
# Board crew has specific preferences and doesn't want to express
# them with command line switches, set them here and you're done.
# In particular, setting the REVIEWBOARD_URL variable will allow
# you to make it easy for people to submit reviews regardless of
# their SCM setup.
#
# Note that in order for this script to work with a reviewboard site
# that uses local paths to access a repository, the 'Mirror path'
# in the repository setup page must be set to the remote URL of the
# repository.

#
# Reviewboard URL.
#
# Set this if you wish to hard-code a default server to always use.
# It's generally recommended to set this using your SCM repository
# (for those that support it -- currently only SVN, Git, and Perforce).
#
# For example, on SVN:
#   $ svn propset reviewboard:url http://reviewboard.example.com .
#
# Or with Git:
#   $ git config reviewboard.url http://reviewboard.example.com
#
# On Perforce servers version 2008.1 and above:
#   $ p4 counter reviewboard.url http://reviewboard.example.com
#
# Older Perforce servers only allow numerical counters, so embedding
# the url in the counter name is also supported:
#   $ p4 counter reviewboard.url.http:\|\|reviewboard.example.com 1
#
# Note that slashes are not allowed in Perforce counter names, so replace them
# with pipe characters (they are a safe substitute as they are not used
# unencoded in URLs). You may need to escape them when issuing the p4 counter
# command as above.
#
# If this is not possible or desired, setting the value here will let
# you get started quickly.
#
# For all other repositories, a .reviewboardrc file present at the top of
# the checkout will also work. For example:
#
#   $ cat .reviewboardrc
#   REVIEWBOARD_URL = "http://reviewboard.example.com"
#
#REVIEWBOARD_URL = None
REVIEWBOARD_URL = "https://reviewboard.wdf.sap.corp/"

# Default submission arguments.  These are all optional; run this
# script with --help for descriptions of each argument.
TARGET_GROUPS = None
TARGET_PEOPLE = None
SUBMIT_AS = None
PUBLISH = False
OPEN_BROWSER = False

# Debugging.  For development...
DEBUG = False

###
# End user-settable variables.
###

user_config = None
tempfiles = []
options = None
frame = None

mainThread = None
uiSemaphore = threading.Semaphore(0)
login_data = None
basepath = None

# config storage
config = wx.Config("Post Review", "Review Board")


class APIError(Exception):
    pass


class RepositoryInfo:
    """
    A representation of a source code repository.
    """
    def __init__(self, path=None, base_path=None, supports_changesets=False,
                 supports_parent_diffs=False):
        self.path = path
        self.base_path = base_path
        self.supports_changesets = supports_changesets
        self.supports_parent_diffs = supports_parent_diffs
        debug("repository info: %s" % self)

    def __str__(self):
        return "Path: %s, Base path: %s, Supports changesets: %s" % \
            (self.path, self.base_path, self.supports_changesets)

    def set_base_path(self, base_path):
        if not base_path.startswith('/'):
            base_path = '/' + base_path
        debug("changing repository info base_path from %s to %s" % \
              (self.base_path, base_path))
        self.base_path = base_path

    def find_server_repository_info(self, server):
        """
        Try to find the repository from the list of repositories on the server.
        For Subversion, this could be a repository with a different URL. For
        all other clients, this is a noop.
        """
        return self


class SvnRepositoryInfo(RepositoryInfo):
    """
    A representation of a SVN source code repository. This version knows how to
    find a matching repository on the server even if the URLs differ.
    """
    def __init__(self, path, base_path, uuid):
        RepositoryInfo.__init__(self, path, base_path)
        self.uuid = uuid

    def find_server_repository_info(self, server):
        """
        The point of this function is to find a repository on the server that
        matches self, even if the paths aren't the same. (For example, if self
        uses an 'http' path, but the server uses a 'file' path for the same
        repository.) It does this by comparing repository UUIDs. If the
        repositories use the same path, you'll get back self, otherwise you'll
        get a different SvnRepositoryInfo object (with a different path).
        """
        repositories = server.get_repositories()

        for repository in repositories:
            if repository['tool'] != 'Subversion':
                continue

            info = self._get_repository_info(server, repository)

            if not info or self.uuid != info['uuid']:
                continue

            repos_base_path = info['url'][len(info['root_url']):]
            relpath = self._get_relative_path(self.base_path, repos_base_path)
            if relpath:
                return SvnRepositoryInfo(info['url'], relpath, self.uuid)

        # We didn't find a matching repository on the server. We'll just return
        # self and hope for the best.
        return self

    def _get_repository_info(self, server, repository):
        try:
            return server.get_repository_info(repository['id'])
        except APIError, e:
            # If the server couldn't fetch the repository info, it will return
            # code 210. Ignore those.
            # Other more serious errors should still be raised, though.
            rsp = e.args[0]
            if rsp['err']['code'] == 210:
                return None

            raise e

    def _get_relative_path(self, path, root):
        pathdirs = self._split_on_slash(path)
        rootdirs = self._split_on_slash(root)

        # root is empty, so anything relative to that is itself
        if len(rootdirs) == 0:
            return path

        # If one of the directories doesn't match, then path is not relative
        # to root.
        if rootdirs != pathdirs:
            return None

        # All the directories matched, so the relative path is whatever
        # directories are left over. The base_path can't be empty, though, so
        # if the paths are the same, return '/'
        if len(pathdirs) == len(rootdirs):
            return '/'
        else:
            return '/'.join(pathdirs[len(rootdirs):])

    def _split_on_slash(self, path):
        # Split on slashes, but ignore multiple slashes and throw away any
        # trailing slashes.
        split = re.split('/*', path)
        if split[ - 1] == '':
            split = split[0: - 1]
        return split


class ReviewBoardHTTPPasswordMgr(urllib2.HTTPPasswordMgr):
    """
    Adds HTTP authentication support for URLs.

    Python 2.4's password manager has a bug in http authentication when the
    target server uses a non-standard port.  This works around that bug on
    Python 2.4 installs. This also allows post-review to prompt for passwords
    in a consistent way.

    See: http://bugs.python.org/issue974757
    """
    def __init__(self, reviewboard_url):
        self.passwd = {}
        self.rb_url = reviewboard_url
        self.rb_user = None
        self.rb_pass = None

    def find_user_password(self, realm, uri):
        if uri.startswith(self.rb_url):
            if self.rb_user is None or self.rb_pass is None:
                if options.gui:
                    credentials = get_login_data(user = self.rb_user, password = self.rb_pass)
                    if not credentials is None:
                        self.rb_user = credentials.user
                        self.rb_pass = credentials.password
                else:
                    print "==> HTTP Authentication Required"
                    print 'Enter username and password for "%s" at %s' % \
                        (realm, urlparse(uri)[1])
                    self.rb_user = raw_input('Username: ')
                    self.rb_pass = getpass.getpass('Password: ')

            return self.rb_user, self.rb_pass
        else:
            # If this is an auth request for some other domain (since HTTP
            # handlers are global), fall back to standard password management.
            return urllib2.HTTPPasswordMgr.find_user_password(self, realm, uri)


class ReviewBoardServer(object):
    """
    An instance of a Review Board server.
    """
    def __init__(self, url, info, cookie_file):
        self.url = url
        if self.url[ - 1] != '/':
            self.url += '/'
        self._info = info
        self._server_info = None
        self.cookie_file = cookie_file
        self.cookie_jar = cookielib.MozillaCookieJar(self.cookie_file)

        # Set up the HTTP libraries to support all of the features we need.
        cookie_handler = urllib2.HTTPCookieProcessor(self.cookie_jar)
        password_mgr = ReviewBoardHTTPPasswordMgr(self.url)
        auth_handler = urllib2.HTTPBasicAuthHandler(password_mgr)
        proxy_support = urllib2.ProxyHandler({})

        opener = urllib2.build_opener(cookie_handler, auth_handler, proxy_support)
        opener.addheaders = [('User-agent', 'post-review/' + constants.VERSION)]
        urllib2.install_opener(opener)

    def login(self):
        """
        Logs in to a Review Board server, prompting the user for login
        information if needed.
        """
        if self.has_valid_cookie():
            return

        username = None
        password = None

        if options.gui:
            if options.username:
                username = options.username
            elif options.submit_as:
                username = options.submit_as

            if options.password:
                password = options.password

            if username is None or password is None:
                credentials = get_login_data(user = username, password = password)
                if not credentials is None:
                    username = credentials.user
                    password = credentials.password
        else:
            print "==> Review Board Login Required"
            print "Enter username and password for Review Board at %s" % self.url
            if options.username:
                username = options.username
            elif options.submit_as:
                username = options.submit_as
            else:
                username = raw_input('Username: ')

            if not options.password:
                password = getpass.getpass('Password: ')
            else:
                password = options.password

        debug('Logging in with username "%s"' % username)
        try:
            self.api_post('api/json/accounts/login/', {
                'username': username,
                'password': password,
            })
        except APIError, e:
            rsp, = e.args

            die("Unable to log in: %s (%s)" % (rsp["err"]["msg"],
                                               rsp["err"]["code"]))

        debug("Logged in.")

    def has_valid_cookie(self):
        """
        Load the user's cookie file and see if they have a valid
        'rbsessionid' cookie for the current Review Board server.  Returns
        true if so and false otherwise.
        """
        try:
            parsed_url = urlparse(self.url)
            host = parsed_url[1]
            path = parsed_url[2] or '/'

            # Cookie files don't store port numbers, unfortunately, so
            # get rid of the port number if it's present.
            host = host.split(":")[0]

            debug("Looking for '%s %s' cookie in %s" % \
                  (host, path, self.cookie_file))
            self.cookie_jar.load(self.cookie_file, ignore_expires=True)

            try:
                cookie = self.cookie_jar._cookies[host][path]['rbsessionid']

                if not cookie.is_expired():
                    debug("Loaded valid cookie -- no login required")
                    return True

                debug("Cookie file loaded, but cookie has expired")
            except KeyError:
                debug("Cookie file loaded, but no cookie for this server")
        except IOError, error:
            debug("Couldn't load cookie file: %s" % error)

        return False

    def new_review_request(self, changenum, submit_as=None):
        """
        Creates a review request on a Review Board server, updating an
        existing one if the changeset number already exists.

        If submit_as is provided, the specified user name will be recorded as
        the submitter of the review request (given that the logged in user has
        the appropriate permissions).
        """
        try:
            debug("Attempting to create review request for %s" % changenum)
            data = { 'repository_path': self.info.path }

            if changenum:
                data['changenum'] = changenum

            if submit_as:
                debug("Submitting the review request as %s" % submit_as)
                data['submit_as'] = submit_as

            rsp = self.api_post('api/json/reviewrequests/new/', data)
        except APIError, e:
            rsp, = e.args

            if not options.diff_only:
                if rsp['err']['code'] == 204: # Change number in use
                    debug("Review request already exists. Updating it...")
                    rsp = self.api_post(
                        'api/json/reviewrequests/%s/update_from_changenum/' % 
                        rsp['review_request']['id'])
                else:
                    raise e

        debug("Review request created")
        return rsp['review_request']

    def set_review_request_field(self, review_request, field, value):
        """
        Sets a field in a review request to the specified value.
        """
        rid = review_request['id']

        debug("Attempting to set field '%s' to '%s' for review request '%s'" % 
              (field, value, rid))

        self.api_post('api/json/reviewrequests/%s/draft/set/' % rid, {
            field: value,
        })

    def get_review_request(self, rid):
        """
        Returns the review request with the specified ID.
        """
        rsp = self.api_get('api/json/reviewrequests/%s/' % rid)
        return rsp['review_request']

    def get_repositories(self):
        """
        Returns the list of repositories on this server.
        """
        rsp = self.api_get('/api/json/repositories/')
        return rsp['repositories']

    def get_repository_info(self, rid):
        """
        Returns detailed information about a specific repository.
        """
        rsp = self.api_get('/api/json/repositories/%s/info/' % rid)
        return rsp['info']

    def save_draft(self, review_request):
        """
        Saves a draft of a review request.
        """
        self.api_post("api/json/reviewrequests/%s/draft/save/" % 
                      review_request['id'])
        debug("Review request draft saved")

    def upload_diff(self, review_request, diff_content, parent_diff_content):
        """
        Uploads a diff to a Review Board server.
        """
        debug("Uploading diff, size: %d" % len(diff_content))

        if parent_diff_content:
            debug("Uploading parent diff, size: %d" % len(parent_diff_content))

        fields = {}
        files = {}

        if self.info.base_path:
            fields['basedir'] = self.info.base_path

        files['path'] = {
            'filename': 'diff',
            'content': diff_content
        }

        if parent_diff_content:
            files['parent_diff_path'] = {
                'filename': 'parent_diff',
                'content': parent_diff_content
            }

        self.api_post('api/json/reviewrequests/%s/diff/new/' % 
                      review_request['id'], fields, files)

    def publish(self, review_request):
        """
        Publishes a review request.
        """
        debug("Publishing")
        self.api_post('api/json/reviewrequests/%s/publish/' % 
                      review_request['id'])

    def _get_server_info(self):
        if not self._server_info:
            self._server_info = self._info.find_server_repository_info(self)

        return self._server_info

    info = property(_get_server_info)

    def process_json(self, data):
        """
        Loads in a JSON file and returns the data if successful. On failure,
        APIError is raised.
        """
        rsp = simplejson.loads(data)

        if rsp['stat'] == 'fail':
            raise APIError, rsp

        return rsp

    def http_get(self, path):
        """
        Performs an HTTP GET on the specified path, storing any cookies that
        were set.
        """
        debug('HTTP GETting %s' % path)

        url = self._make_url(path)

        try:
            rsp = urllib2.urlopen(url).read()
            self.cookie_jar.save(self.cookie_file)
            return rsp
        except urllib2.HTTPError, e:
            error("Unable to access %s (%s). The host path may be invalid" % \
                (url, e.code))
            try:
                debug(e.read())
            except AttributeError:
                pass
            die()

    def _make_url(self, path):
        """Given a path on the server returns a full http:// style url"""
        app = urlparse(self.url)[2]
        if path[0] == '/':
            url = urljoin(self.url, app[: - 1] + path)
        else:
            url = urljoin(self.url, app + path)

        if not url.startswith('http'):
            url = 'http://%s' % url
        return url

    def api_get(self, path):
        """
        Performs an API call using HTTP GET at the specified path.
        """
        return self.process_json(self.http_get(path))

    def http_post(self, path, fields, files=None):
        """
        Performs an HTTP POST on the specified path, storing any cookies that
        were set.
        """
        if fields:
            debug_fields = fields.copy()
        else:
            debug_fields = {}

        if 'password' in debug_fields:
            debug_fields["password"] = "**************"
        url = self._make_url(path)
        debug('HTTP POSTing to %s: %s' % (url, debug_fields))

        content_type, body = self._encode_multipart_formdata(fields, files)
        headers = {
            'Content-Type': content_type,
            'Content-Length': str(len(body))
        }
        
        # debug("headers = %s" % headers)
        # debug("body = %s" % body)

        try:
            r = urllib2.Request(url, body, headers)
            data = urllib2.urlopen(r).read()
            self.cookie_jar.save(self.cookie_file)
            return data
        except urllib2.URLError, e:
            try:
                debug(e.read())
            except AttributeError:
                pass

            die("Unable to access %s. The host path may be invalid\n%s" % \
                (url, e))
        except urllib2.HTTPError, e:
            die("Unable to access %s (%s). The host path may be invalid\n%s" % \
                (url, e.code, e.read()))

    def api_post(self, path, fields=None, files=None):
        """
        Performs an API call using HTTP POST at the specified path.
        """
        debug("Posting API request: path=%s, fields=%s, files=%s" % (path, fields, files))
        return self.process_json(self.http_post(path, fields, files))

    def _encode_multipart_formdata(self, fields, files):
        """
        Encodes data for use in an HTTP POST.
        """
        BOUNDARY = mimetools.choose_boundary()
        content = ""

        fields = fields or {}
        files = files or {}

        for key in fields:
            content += "--" + BOUNDARY + "\r\n"
            content += "Content-Disposition: form-data; name=\"%s\"\r\n" % key
            content += "\r\n"
            content += fields[key] + "\r\n"

        for key in files:
            filename = files[key]['filename']
            value = files[key]['content']
            content += "--" + BOUNDARY + "\r\n"
            content += "Content-Disposition: form-data; name=\"%s\"; " % key
            content += "filename=\"%s\"\r\n" % filename
            content += "\r\n"
            content += value + "\r\n"

        content += "--" + BOUNDARY + "--\r\n"
        content += "\r\n"

        content_type = "multipart/form-data; boundary=%s" % BOUNDARY

        return content_type, content.encode("utf-8")



def execute(command, env=None, split_lines=False, ignore_errors=False,
            extra_ignore_errors=(), p4_login_fix=False, feed_stdin=None):
    """
    Utility function to execute a command and return the output.
    """
    if isinstance(command, list):
        debug(subprocess.list2cmdline(command))
    else:
        debug(command)

    if env:
        env.update(os.environ)
    else:
        env = os.environ.copy()

    env['LC_ALL'] = 'en_US.UTF-8'
    env['LANGUAGE'] = 'en_US.UTF-8'

    if sys.platform.startswith('win'):
        if sys.path[0]:
            env['PATH'] = "%s\\bin;%s" % (os.path.dirname(sys.path[0]), env['PATH'])
        
        p = subprocess.Popen(command,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             shell=True,
                             universal_newlines=True,
                             env=env,
                             creationflags=0x08000000)
    else:
        if sys.path[0]:
            env['PATH'] = "%s/bin:%s" % (os.path.dirname(sys.path[0]), env['PATH'])

        p = subprocess.Popen(command,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             shell=True,
                             close_fds=True,
                             universal_newlines=True,
                             env=env)
    if feed_stdin:
        p.stdin.write(feed_stdin)
        p.stdin.write('\n')
    if split_lines:
        data = p.stdout.readlines()
    else:
        data = p.stdout.read()
    rc = p.wait()
    if rc and not ignore_errors and rc not in extra_ignore_errors:
        if p4_login_fix and len(data) > 0 and (data[0].startswith('Your session has expired, please login again.') or data[0].startswith('Perforce password')):
            if options.gui:
                password = wx.GetPasswordFromUser("Your Perforce session has expired. Please log in again.\n\nPassword:", caption = "Post Review", parent = frame)
            else:
                print "Your Perforce session has expired. Please log in again.\n\nPassword: "
                password = getpass.getpass()
            if password:
                execute(['p4', 'login'], env, feed_stdin = password)
            return execute(command, env, split_lines, ignore_errors, extra_ignore_errors, False, feed_stdin)
        else:
            die('Failed to execute command: %s\n%s' % (command, data))

    return data

class SCMChange(object):
    def __init__(self, id, description, branch = None):
        self.id = id
        self.description = description
        self.branch = branch


class SCMClient(object):
    """
    A base representation of an SCM tool for fetching repository information
    and generating diffs.
    """
    def get_repository_info(self):
        return None

    def scan_for_server(self, repository_info):
        """
        Scans the current directory on up to find a .reviewboard file
        containing the server path.
        """
        server_url = self._get_server_from_config(user_config, repository_info)
        if server_url:
            return server_url

        for path in walk_parents(os.getcwd()):
            filename = os.path.join(path, ".reviewboardrc")
            if os.path.exists(filename):
                config = load_config_file(filename)
                server_url = self._get_server_from_config(config,
                                                          repository_info)
                if server_url:
                    return server_url

        return None

    def diff(self, args):
        """
        Returns the generated diff and optional parent diff for this
        repository.

        The returned tuple is (diff_string, parent_diff_string, branch)
        """
        return (None, None, None)

    def diff_between_revisions(self, revision_range, args, repository_info):
        """
        Returns the generated diff between revisions in the repository.
        """
        return None

    def get_open_changes(self, include_submitted):
        return None

    def _get_server_from_config(self, config, repository_info):
        if 'REVIEWBOARD_URL' in config:
            return config['REVIEWBOARD_URL']
        elif 'TREES' in config:
            trees = config['TREES']
            if not isinstance(trees, dict):
                die("Warning: 'TREES' in config file is not a dict!")

            if repository_info.path in trees and \
               'REVIEWBOARD_URL' in trees[repository_info.path]:
                return trees[repository_info.path]['REVIEWBOARD_URL']

        return None
        
        
class CVSClient(SCMClient):
    """
    A wrapper around the cvs tool that fetches repository
    information and generates compatible diffs.
    """
    def get_repository_info(self):
        if not check_install("cvs"):
            return None

        cvsroot_path = os.path.join("CVS", "Root")

        if not os.path.exists(cvsroot_path):
            return None

        fp = open(cvsroot_path, "r")
        repository_path = fp.read().strip()
        fp.close()

        i = repository_path.find("@")
        if i != - 1:
            repository_path = repository_path[i + 1:]

        i = repository_path.find(":")
        if i != - 1:
            host = repository_path[:i]
            try:
                canon = socket.getfqdn(host)
                repository_path = repository_path.replace('%s:' % host,
                                                          '%s:' % canon)
            except socket.error, msg:
                debug("failed to get fqdn for %s, msg=%s" % (host, msg))

        return RepositoryInfo(path=repository_path)

    def diff(self, files):
        """
        Performs a diff across all modified files in a CVS repository.

        CVS repositories do not support branches of branches in a way that
        makes parent diffs possible, so we never return a parent diff
        (the second value in the tuple).
        """
        return (self.do_diff(files), None, None)

    def diff_between_revisions(self, revision_range, args, repository_info):
        """
        Performs a diff between 2 revisions of a CVS repository.
        """
        revs = []

        for rev in revision_range.split(":"):
            revs += ["-r", rev]

        return self.do_diff(revs)

    def do_diff(self, params):
        """
        Performs the actual diff operation through cvs diff, handling
        fake errors generated by CVS.
        """
        # Diff returns "1" if differences were found.
        return execute(["cvs", "diff", "-uN"] + params,
                        extra_ignore_errors=(1,))


class ClearCaseClient(SCMClient):
    """
    A wrapper around the clearcase tool that fetches repository
    information and generates compatible diffs.
    This client assumes that cygwin is installed on windows.
    """
    ccroot_path = "/view/reviewboard.diffview/vobs/"
    viewinfo = ""
    viewtype = "snapshot"

    def get_filename_hash(self, fname):
        # Hash the filename string so its easy to find the file later on.
        return md5(fname).hexdigest()

    def get_repository_info(self):
        # We must be running this from inside a view.
        # Otherwise it doesn't make sense.
        self.viewinfo = execute(["cleartool", "pwv", "-short"])
        if self.viewinfo.startswith('\*\* NONE'):
            return None

        # Returning the hardcoded clearcase root path to match the server
        #   respository path.
        # There is no reason to have a dynamic path unless you have
        #   multiple clearcase repositories. This should be implemented.
        return RepositoryInfo(path=self.ccroot_path,
                              base_path=self.ccroot_path,
                              supports_parent_diffs=False)

    def get_previous_version(self, files):
        file = []
        curdir = os.getcwd()

        # Cygwin case must transform a linux-like path to windows like path
        #   including drive letter.
        if 'cygdrive' in curdir:
            where = curdir.index('cygdrive') + 9
            drive_letter = curdir[where:where + 1]
            curdir = drive_letter + ":\\" + curdir[where + 2:len(curdir)]

        for key in files:
            # Sometimes there is a quote in the filename. It must be removed.
            key = key.replace('\'', '')
            elem_path = cpath.normpath(os.path.join(curdir, key))

            # Removing anything before the last /vobs
            #   because it may be repeated.
            elem_path_idx = elem_path.rfind("/vobs")
            if elem_path_idx != - 1:
                elem_path = elem_path[elem_path_idx:len(elem_path)].strip("\"")

            # Call cleartool to get this version and the previous version
            #   of the element.
            curr_version, pre_version = execute(
                ["cleartool", "desc", "-pre", elem_path])
            curr_version = cpath.normpath(curr_version)
            pre_version = pre_version.split(':')[1].strip()

            # If a specific version was given, remove it from the path
            #   to avoid version duplication
            if "@@" in elem_path:
                elem_path = elem_path[:elem_path.rfind("@@")]
            file.append(elem_path + "@@" + pre_version)
            file.append(curr_version)

        # Determnine if the view type is snapshot or dynamic.
        if os.path.exists(file[0]):
            self.viewtype = "dynamic"

        return file

    def get_extended_namespace(self, files):
        """
        Parses the file path to get the extended namespace
        """
        versions = self.get_previous_version(files)

        evfiles = []
        hlist = []

        for vkey in versions:
            # Verify if it is a checkedout file.
            if "CHECKEDOUT" in vkey:
                # For checkedout files just add it to the file list
                #   since it cannot be accessed outside the view.
                splversions = vkey[:vkey.rfind("@@")]
                evfiles.append(splversions)
            else:
                # For checkedin files.
                ext_path = []
                ver = []
                fname = ""      # fname holds the file name without the version.
                (bpath, fpath) = cpath.splitdrive(vkey)
                if bpath :
                    # Windows.
                    # The version (if specified like file.c@@/main/1)
                    #   should be kept as a single string
                    #   so split the path and concat the file name
                    #   and version in the last position of the list.
                    ver = fpath.split("@@")
                    splversions = fpath[:vkey.rfind("@@")].split("\\")
                    fname = splversions.pop()
                    splversions.append(fname + ver[1])
                else :
                    # Linux.
                    bpath = vkey[:vkey.rfind("vobs") + 4]
                    fpath = vkey[vkey.rfind("vobs") + 5:]
                    ver = fpath.split("@@")
                    splversions = ver[0][:vkey.rfind("@@")].split("/")
                    fname = splversions.pop()
                    splversions.append(fname + ver[1])

                filename = splversions.pop()
                bpath = cpath.normpath(bpath + "/")
                elem_path = bpath

                for key in splversions:
                    # For each element (directory) in the path,
                    #   get its version from clearcase.
                    elem_path = cpath.join(elem_path, key)

                    # This is the version to be appended to the extended
                    #   path list.
                    this_version = execute(
                        ["cleartool", "desc", "-fmt", "%Vn",
                        cpath.normpath(elem_path)])
                    if this_version:
                        ext_path.append(key + "/@@" + this_version + "/")
                    else:
                        ext_path.append(key + "/")

                # This must be done in case we haven't specified
                #   the version on the command line.
                ext_path.append(cpath.normpath(fname + "/@@" + 
                    vkey[vkey.rfind("@@") + 2:len(vkey)]))
                epstr = cpath.join(bpath, cpath.normpath(''.join(ext_path)))
                evfiles.append(epstr)

                """
                In windows, there is a problem with long names(> 254).
                In this case, we hash the string and copy the unextended
                  filename to a temp file whose name is the hash.
                This way we can get the file later on for diff.
                The same problem applies to snapshot views where the
                  extended name isn't available.
                The previous file must be copied from the CC server
                  to a local dir.
                """
                if cpath.exists(epstr) :
                    pass
                else:
                    if len(epstr) > 254 or self.viewtype == "snapshot":
                        name = self.get_filename_hash(epstr)
                        # Check if this hash is already in the list
                        try:
                            i = hlist.index(name)
                            die("ERROR: duplicate value %s : %s" % 
                                (name, epstr))
                        except ValueError:
                            hlist.append(name)

                        normkey = cpath.normpath(vkey)
                        td = tempfile.gettempdir()
                        # Cygwin case must transform a linux-like path to
                        # windows like path including drive letter
                        if 'cygdrive' in td:
                            where = td.index('cygdrive') + 9
                            drive_letter = td[where:where + 1] + ":"
                            td = cpath.join(drive_letter, td[where + 1:])
                        tf = cpath.normpath(cpath.join(td, name))
                        if cpath.exists(tf):
                            debug("WARNING: FILE EXISTS")
                            os.unlink(tf)
                        execute(["cleartool", "get", "-to", tf, normkey])
                    else:
                        die("ERROR: FILE NOT FOUND : %s" % epstr)

        return evfiles

    def get_files_from_label(self, label):
        voblist = []
        # Get the list of vobs for the current view
        allvoblist = execute(["cleartool", "lsvob", "-short"]).split()
        # For each vob, find if the label is present
        for vob in allvoblist:
            try:
                execute(["cleartool", "describe", "-local",
                    "lbtype:%s@%s" % (label, vob)]).split()
                voblist.append(vob)
            except:
                pass

        filelist = []
        # For each vob containing the label, get the file list
        for vob in voblist:
            try:
                res = execute(["cleartool", "find", vob, "-all", "-version",
                    "lbtype(%s)" % label, "-print"])
                filelist.extend(res.split())
            except :
                pass

        # Return only the unique itens
        return set(filelist)

    def diff(self, files):
        """
        Performs a diff of the specified file and its previous version.
        """
        # We must be running this from inside a view.
        # Otherwise it doesn't make sense.
        return self.do_diff(self.get_extended_namespace(files))

    def diff_label(self, label):
        """
        Get the files that are attached to a label and diff them
        TODO
        """
        return self.diff(self.get_files_from_label(label))

    def diff_between_revisions(self, revision_range, args, repository_info):
        """
        Performs a diff between 2 revisions of a CC repository.
        """
        rev_str = ''

        for rev in revision_range.split(":"):
            rev_str += "-r %s" % rev

        return self.do_diff(rev_str)

    def do_diff(self, params):
        # Diff returns "1" if differences were found.
        # Add the view name and view type to the description
        if options.description:
            options.description = ("VIEW: " + self.viewinfo + 
                "VIEWTYPE: " + self.viewtype + "\n" + options.description)
        else:
            options.description = (self.viewinfo + 
                "VIEWTYPE: " + self.viewtype + "\n")

        o = []
        Feol = False
        while len(params) > 0:
            # Read both original and modified files.
            onam = params.pop(0)
            mnam = params.pop(0)
            file_data = []
            do_rem = False
            # If the filename length is greater than 254 char for windows,
            #   we copied the file to a temp file
            #   because the open will not work for path greater than 254.
            # This is valid for the original and
            #   modified files if the name size is > 254.
            for filenam in (onam, mnam) :
                if cpath.exists(filenam) and self.viewtype == "dynamic":
                    do_rem = False
                    fn = filenam
                elif len(filenam) > 254 or self.viewtype == "snapshot":
                    fn = self.get_filename_hash(filenam)
                    fn = cpath.join(tempfile.gettempdir(), fn)
                    do_rem = True
                fd = open(cpath.normpath(fn))
                fdata = fd.readlines()
                fd.close()
                file_data.append(fdata)
                # If the file was temp, it should be removed.
                if do_rem:
                    os.remove(filenam)

            modi = file_data.pop()
            orig = file_data.pop()

            # For snapshot views, the local directories must be removed because
            #   they will break the diff on the server. Just replacing
            #   everything before the view name (including the view name) for
            #   vobs do the work.
            if (self.viewtype == "snapshot"
                and (sys.platform.startswith('win')
                  or sys.platform.startswith('cygwin'))):
                    vinfo = self.viewinfo.rstrip("\r\n")
                    mnam = "c:\\\\vobs" + mnam[mnam.rfind(vinfo) + len(vinfo):]
                    onam = "c:\\\\vobs" + onam[onam.rfind(vinfo) + len(vinfo):]
            # Call the diff lib to generate a diff.
            # The dates are bogus, since they don't natter anyway.
            # The only thing is that two spaces are needed to the server
            #   so it can identify the heades correctly.
            diff = difflib.unified_diff(orig, modi, onam, mnam,
               '  2002-02-21 23:30:39.942229878 -0800',
               '  2002-02-21 23:30:50.442260588 -0800', lineterm=' \n')
            # Transform the generator output into a string output
            #   Use a comprehension instead of a generator,
            #   so 2.3.x doesn't fail to interpret.
            diffstr = ''.join([str(l) for l in diff])
            # Workaround for the difflib no new line at end of file
            #   problem.
            if not diffstr.endswith('\n'):
                diffstr = diffstr + ("\n\\ No newline at end of file\n")
            o.append(diffstr)

        ostr = ''.join(o)
        return (ostr, None, None) # diff, parent_diff (not supported), branch (not supported)


class SVNClient(SCMClient):
    """
    A wrapper around the svn Subversion tool that fetches repository
    information and generates compatible diffs.
    """
    def get_repository_info(self):
        if not check_install('svn help'):
            return None

        # Get the SVN repository path (either via a working copy or
        # a supplied URI)
        svn_info_params = ["svn", "info"]
        if options.repository_url:
            svn_info_params.append(options.repository_url)
        data = execute(svn_info_params,
                       ignore_errors=True)
        m = re.search(r'^Repository Root: (.+)$', data, re.M)
        if not m:
            return None

        path = m.group(1)

        m = re.search(r'^URL: (.+)$', data, re.M)
        if not m:
            return None

        base_path = m.group(1)[len(path):] or "/"

        m = re.search(r'^Repository UUID: (.+)$', data, re.M)
        if not m:
            return None

        return SvnRepositoryInfo(path, base_path, m.group(1))

    def scan_for_server(self, repository_info):
        # Scan first for dot files, since it's faster and will cover the
        # user's $HOME/.reviewboardrc
        server_url = super(SVNClient, self).scan_for_server(repository_info)
        if server_url:
            return server_url

        return self.scan_for_server_property(repository_info)

    def scan_for_server_property(self, repository_info):
        def get_url_prop(path):
            url = execute(["svn", "propget", "reviewboard:url", path]).strip()
            return url or None

        for path in walk_parents(os.getcwd()):
            if not os.path.exists(os.path.join(path, ".svn")):
                break

            prop = get_url_prop(path)
            if prop:
                return prop

        return get_url_prop(repository_info.path)

    def diff(self, files):
        """
        Performs a diff across all modified files in a Subversion repository.

        SVN repositories do not support branches of branches in a way that
        makes parent diffs possible, so we never return a parent diff
        (the second value in the tuple).
        """
        return (self.do_diff(["svn", "diff", "--diff-cmd=rbdiff"] + files),
                None, None)

    def diff_between_revisions(self, revision_range, args, repository_info):
        """
        Performs a diff between 2 revisions of a Subversion repository.
        """
        if options.repository_url:
            revisions = revision_range.split(':')
            if len(revisions) < 1:
                return None
            elif len(revisions) == 1:
                revisions.append('HEAD')

            # if a new path was supplied at the command line, set it
            if len(args):
                repository_info.set_base_path(args[0])

            url = repository_info.path + repository_info.base_path

            old_url = url + '@' + revisions[0]
            new_url = url + '@' + revisions[1]

            return self.do_diff(["svn", "diff", "--diff-cmd=rbdiff", old_url,
                                 new_url],
                                repository_info)
        # Otherwise, perform the revision range diff using a working copy
        else:
            return self.do_diff(["svn", "diff", "--diff-cmd=rbdiff", "-r",
                                 revision_range],
                                repository_info)

    def do_diff(self, cmd, repository_info=None):
        """
        Performs the actual diff operation, handling renames and converting
        paths to absolute.
        """
        diff = execute(cmd, split_lines=True)
        diff = self.handle_renames(diff)
        diff = self.convert_to_absolute_paths(diff, repository_info)

        return ''.join(diff)

    def handle_renames(self, diff_content):
        """
        The output of svn diff is incorrect when the file in question came
        into being via svn mv/cp. Although the patch for these files are
        relative to its parent, the diff header doesn't reflect this.
        This function fixes the relevant section headers of the patch to
        portray this relationship.
        """

        # svn diff against a repository URL on two revisions appears to
        # handle moved files properly, so only adjust the diff file names
        # if they were created using a working copy.
        if options.repository_url:
            return diff_content

        result = []

        from_line = ""
        for line in diff_content:
            if line.startswith('--- '):
                from_line = line
                continue

            # This is where we decide how mangle the previous '--- '
            if line.startswith('+++ '):
                to_file, _ = self.parse_filename_header(line[4:])
                info = self.svn_info(to_file)
                if info.has_key("Copied From URL"):
                    url = info["Copied From URL"]
                    root = info["Repository Root"]
                    from_file = urllib.unquote(url[len(root):])
                    result.append(from_line.replace(to_file, from_file))
                else:
                    result.append(from_line) #as is, no copy performed

            # We only mangle '---' lines. All others get added straight to
            # the output.
            result.append(line)

        return result


    def convert_to_absolute_paths(self, diff_content, repository_info):
        """
        Converts relative paths in a diff output to absolute paths.
        This handles paths that have been svn switched to other parts of the
        repository.
        """

        result = []

        for line in diff_content:
            front = None
            if line.startswith('+++ ') or line.startswith('--- ') or line.startswith('Index: '):
                front, line = line.split(" ", 1)

            if front:
                if line.startswith('/'): #already absolute
                    line = front + " " + line
                else:
                    # filename and rest of line (usually the revision
                    # component)
                    file, rest = self.parse_filename_header(line)

                    # If working with a diff generated outside of a working
                    # copy, then file paths are already absolute, so just
                    # add initial slash.
                    if options.repository_url:
                        path = urllib.unquote(
                            os.path.join(repository_info.base_path, file))
                    else:
                        info = self.svn_info(file)
                        url = info["URL"]
                        root = info["Repository Root"]
                        path = urllib.unquote(url[len(root):])

                    line = front + " " + path + rest

            result.append(line)

        return result

    def svn_info(self, path):
        """Return a dict which is the result of 'svn info' at a given path."""
        svninfo = {}
        for info in execute(["svn", "info", path],
                            split_lines=True):
            parts = info.strip().split(": ", 1)
            if len(parts) == 2:
                key, value = parts
                svninfo[key] = value

        return svninfo

    # Adapted from server code parser.py
    def parse_filename_header(self, s):
        parts = None
        if "\t" in s:
            # There's a \t separating the filename and info. This is the
            # best case scenario, since it allows for filenames with spaces
            # without much work.
            parts = s.split("\t")

        # There's spaces being used to separate the filename and info.
        # This is technically wrong, so all we can do is assume that
        # 1) the filename won't have multiple consecutive spaces, and
        # 2) there's at least 2 spaces separating the filename and info.
        if "  " in s:
            parts = re.split(r"  +", s)

        if parts:
            parts[1] = '\t' + parts[1]
            return parts

        # strip off ending newline, and return it as the second component
        return [s.split('\n')[0], '\n']


class PerforceClient(SCMClient):
    """
    A wrapper around the p4 Perforce tool that fetches repository information
    and generates compatible diffs.
    """
    def get_repository_info(self):
        if not check_install('p4 help'):
            return None

        data = self.p4_execute(["p4", "info"], ignore_errors=True)

        m = re.search(r'^Server address: (.+)$', data, re.M)
        if not m:
            return None

        repository_path = m.group(1).strip()

        try:
            hostname, port = repository_path.split(":")
            info = socket.gethostbyaddr(hostname)
            repository_path = "%s:%s" % (info[0], port)
        except (socket.gaierror, socket.herror):
            pass

        return RepositoryInfo(path=repository_path, supports_changesets=True)

    def scan_for_server(self, repository_info):
        # Scan first for dot files, since it's faster and will cover the
        # user's $HOME/.reviewboardrc
        server_url = \
            super(PerforceClient, self).scan_for_server(repository_info)

        if server_url:
            return server_url

        return self.scan_for_server_counter(repository_info)

    def scan_for_server_counter(self, repository_info):
        """
        Checks the Perforce counters to see if the Review Board server's url
        is specified. Since Perforce only started supporting non-numeric
        counter values in server version 2008.1, we support both a normal
        counter 'reviewboard.url' with a string value and embedding the url in
        a counter name like 'reviewboard.url.http:||reviewboard.example.com'.
        Note that forward slashes aren't allowed in counter names, so
        pipe ('|') characters should be used. These should be safe because they
        should not be used unencoded in urls.
        """

        counters_text = self.p4_execute(["p4", "counters"])

        # Try for a "reviewboard.url" counter first.
        m = re.search(r'^reviewboard.url = (\S+)', counters_text, re.M)

        if m:
            return m.group(1)

        # Next try for a counter of the form:
        # reviewboard_url.http:||reviewboard.example.com
        m2 = re.search(r'^reviewboard.url\.(\S+)', counters_text, re.M)

        if m2:
            return m2.group(1).replace('|', '/')

        return None

    def diff(self, args):
        """
        Goes through the hard work of generating a diff on Perforce in order
        to take into account adds/deletes and to provide the necessary
        revision information.
        """
        if len(args) != 1:
            error("Specify the change number of a pending changeset")
            sys.exit(1)

        changenum = args[0]

        cl_is_pending = False

        try:
            changenum = int(changenum)
        except ValueError:
            die("You must enter a valid change number")

        debug("Generating diff for changenum %s" % changenum)

        # set the P4 enviroment:
        if options.p4_client:
           os.environ['P4CLIENT'] = options.p4_client

        if options.p4_port:
           os.environ['P4PORT'] = options.p4_port

        description = self.p4_execute(["p4", "describe", "-s", str(changenum)],
                              split_lines=True)

        if '*pending*' in description[0]:
            cl_is_pending = True

        # Get the file list
        for line_num, line in enumerate(description):
            if 'Affected files ...' in line:
                break
        else:
            # Got to the end of all the description lines and didn't find
            # what we were looking for.
            die("Couldn't find any affected files for this change.")

        description = description[line_num + 2:]

        cwd = os.getcwd()
        diff_lines = []

        empty_filename = make_tempfile()
        tmp_diff_from_filename = make_tempfile()
        tmp_diff_to_filename = make_tempfile()

        branch = project = None
        branchdesc = "(none)"

        for line in description:
            line = line.strip()
            if not line:
                continue

            m = re.search(r'\.\.\. ([^#]+)#(\d+) (add|edit|delete|integrate|branch)', line)
            if not m:
                die("Unsupported line from p4 opened: %s" % line)

            depot_path = m.group(1)
            base_revision = int(m.group(2))
            if not cl_is_pending:
                # If the changelist is pending our base revision is the one that's
                # currently in the depot. If we're not pending the base revision is
                # actually the revision prior to this one
                base_revision -= 1

            changetype = m.group(3)

            debug('Processing %s of %s' % (changetype, depot_path))

            local_name = m.group(1)

            m = re.search('\.\.\. //[^/]+/([^/]+)/([^/]+)/', line)
            if m:
                newproject = m.group(1)
                newbranch = m.group(2)
                
                if (not project is None) and newproject != project:
                    if newbranch != branch:
                        branchdesc = '(multiple branches)'
                        break
                    else:
                        branchdesc = '%s/(multiple projects)' % branch
                elif (not branch is None) and newbranch != branch:
                    branchdesc = '(multiple branches)'
                    break
                else:
                    project = newproject
                    branch = newbranch
                    branchdesc = '%s/%s' % (branch, project)

            old_file = new_file = empty_filename
            old_depot_path = new_depot_path = None
            changetype_short = None

            if changetype == 'edit' or changetype == 'integrate':
                # A big assumption
                new_revision = base_revision + 1

                # We have an old file, get p4 to take this old version from the
                # depot and put it into a plain old temp file for us
                old_depot_path = "%s#%s" % (depot_path, base_revision)
                self._write_file(old_depot_path, tmp_diff_from_filename)
                old_file = tmp_diff_from_filename

                # Also print out the new file into a tmpfile
                if cl_is_pending:
                    new_file = self._depot_to_local(depot_path)
                else:
                    new_depot_path = "%s#%s" % (depot_path, new_revision)
                    self._write_file(new_depot_path, tmp_diff_to_filename)
                    new_file = tmp_diff_to_filename

                changetype_short = "M"

            elif changetype == 'add' or changetype == 'branch':
                # We have a new file, get p4 to put this new file into a pretty
                # temp file for us. No old file to worry about here.
                if cl_is_pending:
                    new_file = self._depot_to_local(depot_path)
                else:
                    self._write_file(depot_path, tmp_diff_to_filename)
                    new_file = tmp_diff_to_filename
                changetype_short = "A"

            elif changetype == 'delete':
                # We've deleted a file, get p4 to put the deleted file into  a temp
                # file for us. The new file remains the empty file.
                old_depot_path = "%s#%s" % (depot_path, base_revision)
                self._write_file(old_depot_path, tmp_diff_from_filename)
                old_file = tmp_diff_from_filename
                changetype_short = "D"
            else:
                die("Unknown change type '%s' for %s" % (changetype, depot_path))

            diff_cmd = ["rbdiff", "-urNp", old_file, new_file]
            # Diff returns "1" if differences were found.
            dl = execute(diff_cmd, extra_ignore_errors=(1, 2)).splitlines(True)

            if local_name.startswith(cwd):
                local_path = local_name[len(cwd) + 1:]
            else:
                local_path = local_name

            # Special handling for the output of the diff tool on binary files:
            #     diff outputs "Files a and b differ"
            # and the code below expects the outptu to start with
            #     "Binary files "
            if len(dl) == 1 and \
               (dl[0].startswith('Files %s and %s differ' % (old_file, new_file)) or \
                dl[0].startswith('Files %s and Change differ' % old_file)):
                dl = ["Binary files %s and %s differ\n" % (old_file, new_file)]

            if dl == [] or dl[0].startswith("Binary files "):
                if dl == []:
                    print "Warning: %s in your changeset is unmodified" % \
                        local_path

                dl.insert(0, "==== %s#%s ==%s== %s ====\n" % \
                    (depot_path, base_revision, changetype_short, local_path))
            else:
                m = re.search(r'(\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d)', dl[1])
                if m:
                    timestamp = m.group(1)
                else:
                    # Thu Sep  3 11:24:48 2007
                    m = re.search(r'(\w+)\s+(\w+)\s+(\d+)\s+(\d\d:\d\d:\d\d)\s+(\d\d\d\d)', dl[1])
                    if not m:
                        die("Unable to parse diff header: %s" % dl[1])

                    month_map = {
                        "Jan": "01",
                        "Feb": "02",
                        "Mar": "03",
                        "Apr": "04",
                        "May": "05",
                        "Jun": "06",
                        "Jul": "07",
                        "Aug": "08",
                        "Sep": "09",
                        "Oct": "10",
                        "Nov": "11",
                        "Dec": "12",
                    }
                    month = month_map[m.group(2)]
                    day = m.group(3)
                    timestamp = m.group(4)
                    year = m.group(5)

                    timestamp = "%s-%s-%s %s" % (year, month, day, timestamp)

                dl[0] = "--- %s\t%s#%s\n" % (local_path, depot_path, base_revision)
                dl[1] = "+++ %s\t%s\n" % (local_path, timestamp)

            diff_lines += dl

        os.unlink(empty_filename)
        os.unlink(tmp_diff_from_filename)
        os.unlink(tmp_diff_to_filename)

        return (''.join(diff_lines), None, branchdesc)

    def _write_file(self, depot_path, tmpfile):
        """
        Grabs a file from Perforce and writes it to a temp file. We do this
        wrather than telling p4 print to write it out in order to work around
        a permissions bug on Windows.
        """
        debug('Writing "%s" to "%s"' % (depot_path, tmpfile))
        data = self.p4_execute(["p4", "print", "-q", depot_path])

        f = open(tmpfile, "w")
        f.write(data)
        f.close()

    def _depot_to_local(self, depot_path):
        """
        Given a path in the depot return the path on the local filesystem to
        the same file.
        """
        # $ p4 where //user/bvanzant/main/testing
        # //user/bvanzant/main/testing //bvanzant:test05:home/testing /home/bvanzant/home-versioned/testing
        where_output = self.p4_execute(["p4", "where", depot_path], split_lines=True)
        # Take only the last line from the where command.  If you have a
        # multi-line view mapping with exclusions, Perforce will display
        # the exclusions in order, with the last line showing the actual
        # location.
        last_line = where_output[ - 1].strip()

        # XXX: This breaks on filenames with spaces.
        return last_line.split(' ')[2].rstrip()

    def get_open_changes(self, include_submitted):
        # set the P4 enviroment:
        if options.p4_client:
           os.environ['P4CLIENT'] = options.p4_client

        if options.p4_port:
           os.environ['P4PORT'] = options.p4_port

        user = string.lower(get_scm_user(config, options))
        
        cmd = ['p4', 'changes', '-L']
        if not include_submitted:
            cmd = cmd + ['-s', 'pending']
        cmd = cmd + ['-m', str(config.ReadInt(constants.CONFIG_SCM_MAX_P4_CL_COUNT, constants.DEFAULT_CONFIG_SCM_MAX_P4_CL_COUNT)), '-u', user]
        
        changes = self.p4_execute(cmd, split_lines = True)

        result = []
        changeid = None
        desc = None

        for line in changes:
            m = re.search(r'Change (\d+)', line)
            if m:
                if changeid and desc:
                    result.append(SCMChange(changeid, desc, self.get_branch(changeid)))
                    changeid = None
                    desc = None
                changeid = m.group(1)
            else:
                m = re.search(r'\t(.[^\r\n]+)', line)
                if m:
                    if desc:
                        desc = desc + ' ' + m.group(1)
                    else:
                        desc = m.group(1)

        if changeid and desc:
            result.append(SCMChange(changeid, desc, self.get_branch(changeid)))

        return result

    def get_branch(self, changeid):
        changelist = self.p4_execute(['p4', 'describe', '-s', changeid], split_lines = True)

        branch = project = None
        branchdesc = "(none)"
        for ln in changelist:
            m = re.search('\.\.\. //[^/]+/([^/]+)/([^/]+)/', ln)
            if m:
                newproject = m.group(1)
                newbranch = m.group(2)
                
                if (not project is None) and newproject != project:
                    if newbranch != branch:
                        branchdesc = '(multiple branches)'
                        break
                    else:
                        branchdesc = '%s/(multiple projects)' % branch
                elif (not branch is None) and newbranch != branch:
                    branchdesc = '(multiple branches)'
                    break
                else:
                    project = newproject
                    branch = newbranch
                    branchdesc = '%s/%s' % (branch, project)
        
        return branchdesc

    def p4_execute(self, command, env=None, split_lines=False, ignore_errors=False,
                   extra_ignore_errors=()):
        return execute(command, env=env, split_lines=split_lines, ignore_errors=ignore_errors,
                         extra_ignore_errors=extra_ignore_errors, p4_login_fix=True)


"""
A minimal implementation of the SAP DTR protocol that fetches repository information
and generates compatible diffs.
"""
class DtrClient(DtrBaseClient, SCMClient):
    def __init__(self):
        SCMClient.__init__(self)
        DtrBaseClient.__init__(self, get_dtr_server(config, options), constants.DTR_USER, constants.DTR_PASSWORD)

    def get_repository_info(self):
        return RepositoryInfo(path=get_dtr_server(config, options), supports_changesets=False)

    def scan_for_server(self, repository_info):
        # Scan first for dot files, since it's faster and will cover the
        # user's $HOME/.reviewboardrc
        server_url = \
            super(DtrClient, self).scan_for_server(repository_info)

        if server_url:
            return server_url

        return self.scan_for_server_counter(repository_info)

    def scan_for_server_counter(self, repository_info):
        return None

    def diff(self, args):
        """
        Goes through the hard work of generating a diff for DTR in order
        to take into account adds/deletes and to provide the necessary
        revision information.
        """
        if len(args) != 1:
            print >> sys.stderr, "Specify the name of an activity"
            sys.exit(1)

        activity = args[0]

        act_is_open = False

        debug("Generating diff for activity %s" % activity)

        print >> sys.stderr, "Repository: %s" % self.get_repository_info()

        act = self.dtr_get_activity("/dtr/act/%s" % activity)
        options.summary = options.description = act.displayname

        branchdesc = "(none)"
        if act.get_workspace():
            m2 = re.search('/ws/([^/]+)/([^_/]+)_([^/]+)/([^/]+)/', act.get_workspace().get_path())
            if m2:
                branchdesc = "%s_%s/%s" % (m2.group(1), m2.group(4), m2.group(3))

        print >> sys.stderr, "Integrations: %s" % act.get_integrations()
        
        integration = act.get_oldest_integration()

        print >> sys.stderr, "Oldest integration: %s" % integration
        
        cwd = os.getcwd()
        diff_lines = []

        print >> sys.stderr, "***************************************"
        print >> sys.stderr, act
        print >> sys.stderr, "***************************************"

        # create temp files
        empty_filename = make_tempfile()
        tmp_diff_from_filename = make_tempfile()
        tmp_diff_to_filename = make_tempfile()

        changes = act.get_version_set() + act.get_content_set();
        for version in changes:
            if type(version) == DtrVersion:
                depot_path = "%s/byintegration/all/%s%s" % (act.get_workspace().get_history(), integration.get_isn() - 1, version.get_path())
            elif type(version) == DtrFile or type(version) == DtrCollection:
                depot_path = "%s%s" % (act.get_workspace().path, version.get_path())
            else:
                depot_path = "%s#%s%s" % (version.get_most_recent_predecessor().get_resource_path(), act.get_workspace().path, version.get_path())
                #depot_path = "%s%s" % (act.get_workspace().path, version.get_path())

            if integration:
                base_revision = integration.get_isn() - 1
            elif type(version) == DtrFile or type(version) == DtrCollection:
                base_revision = version.get_revision() - 1
            else:
                print version
                dtr_base_version = self._dtr_get_resource(version.get_most_recent_predecessor().get_resource_path())
                base_revision = dtr_base_version.get_revision()

            if version.is_created():
                changetype = "create"
            elif version.is_deleted():
                changetype = "delete"
            else:
                changetype = "edit"

            if version.is_directory():
                print >> sys.stderr, 'Skipping %s of %s (is a directory)' % (changetype, depot_path)
            else:
                print >> sys.stderr, 'Processing %s of %s' % (changetype, depot_path)
                old_file = new_file = empty_filename

                old_depot_path = new_depot_path = None
                changetype_short = None

                if version.is_created():
                    # We have a new file, get p4 to put this new file into a pretty
                    # temp file for us. No old file to worry about here.
                    new_file = tmp_diff_to_filename
                    self._dtr_get_file(act, version, new_file, False)
                    changetype_short = "A"
                elif version.is_deleted():
                    # We've deleted a file, get p4 to put the deleted file into  a temp
                    # file for us. The new file remains the empty file.
                    old_file = tmp_diff_from_filename
                    self._dtr_get_file(act, version, old_file, True)
                    changetype_short = "D"
                else: # Edit
                    # get predecessor
                    old_file = tmp_diff_from_filename
                    self._dtr_get_file(act, version, old_file, True)
                    # Also print out the new file into a tmpfile
                    new_file = tmp_diff_to_filename
                    self._dtr_get_file(act, version, new_file, False)
                    changetype_short = "M"

                diff_cmd = ["rbdiff", "-urNp", old_file, new_file]
                # Diff returns "1" if differences were found.
                dl = execute(diff_cmd, extra_ignore_errors=(1, 2)).splitlines(True)

                local_path = version.get_name()

                # Special handling for the output of the diff tool on binary files:
                #     diff outputs "Files a and b differ"
                # and the code below expects the outptu to start with
                #     "Binary files "
                if len(dl) == 1 and \
                   dl[0].startswith('Files %s and %s differ' % (old_file, new_file)):
                    dl = ["Binary files %s and %s differ\n" % (old_file, new_file)]

                if dl == [] or dl[0].startswith("Binary files "):
                    if dl == []:
                        print >> sys.stderr, "Warning: %s in your changeset is unmodified" % \
                            local_path

                    dl.insert(0, "==== %s#%s ==%s== %s ====\n" % \
                        (depot_path, base_revision, changetype_short, local_path))
                else:
                    m = re.search(r'(\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d)', dl[1])
                    if m:
                        timestamp = m.group(1)
                    else:
                        # Thu Sep  3 11:24:48 2007
                        m = re.search(r'(\w+)\s+(\w+)\s+(\d+)\s+(\d\d:\d\d:\d\d)\s+(\d\d\d\d)', dl[1])
                        if not m:
                            die("Unable to parse diff header: %s" % dl[1])

                        month_map = {
                            "Jan": "01",
                            "Feb": "02",
                            "Mar": "03",
                            "Apr": "04",
                            "May": "05",
                            "Jun": "06",
                            "Jul": "07",
                            "Aug": "08",
                            "Sep": "09",
                            "Oct": "10",
                            "Nov": "11",
                            "Dec": "12",
                        }
                        month = month_map[m.group(2)]
                        day = m.group(3)
                        timestamp = m.group(4)
                        year = m.group(5)

                        timestamp = "%s-%s-%s %s" % (year, month, day, timestamp)

                    dl[0] = "--- %s\t%s#%s\n" % (local_path, depot_path, base_revision)
                    dl[1] = "+++ %s\t%s\n" % (local_path, timestamp)

                diff_lines += "Index: %s\n===================================================================\n" % version.path
                diff_lines += dl

        os.unlink(empty_filename)
        os.unlink(tmp_diff_from_filename)
        os.unlink(tmp_diff_to_filename)

        return (''.join(diff_lines), None, branchdesc)
        
    def get_open_changes(self, include_submitted):
        user = string.upper(get_scm_user(config, options))
        activities = self.dtr_get_activities(user = user) #, closed = include_submitted, max_age = config.ReadInt(constants.CONFIG_SCM_MAX_DTR_ACT_AGE, constants.DEFAULT_CONFIG_SCM_MAX_DTR_ACT_AGE))
        result = []
        for act in activities:
            m = re.search('/dtr/act/(.*)', act.get_resource_path())
            # only get changes for the current client
            if act.get_client_hostname() and string.upper(act.get_client_hostname()) == string.upper(os.environ['COMPUTERNAME']):
                # only show non-empty activities
                #if act.get_vset_element_count() > 0:
                branch = None
                if act.get_workspace():
                    m2 = re.search('/ws/([^/]+)/([^_/]+)_([^/]+)/([^/]+)/', act.get_workspace())
                    if m2:
                        branch = "%s_%s/%s" % (m2.group(1), m2.group(4), m2.group(3))
                result.append(SCMChange(m.group(1), act.get_display_name(), branch))
        return result


class MercurialClient(SCMClient):
    """
    A wrapper around the hg Mercurial tool that fetches repository
    information and generates compatible diffs.
    """
    def get_repository_info(self):
        if not check_install('hg --help'):
            return None

        data = execute(["hg", "root"], ignore_errors=True)
        if data.startswith('abort:'):
            # hg aborted => no mercurial repository here.
            return None

        # Elsewhere, hg root output give us the repository path.

        # We save data here to use it as a fallback. See below
        local_data = data.strip()

        svn = execute(["hg", "svn", "info", ], ignore_errors=True)

        if not svn.startswith('abort:'):
            self.type = 'svn'
            m = re.search(r'^Repository Root: (.+)$', svn, re.M)

            if not m:
                return None

            path = m.group(1)
            m2 = re.match(r'^(svn\+ssh|http|https)://([-a-zA-Z0-9.]*@)(.*)$',
                          path)
            if m2:
                path = '%s://%s' % (m2.group(1), m2.group(3))

            m = re.search(r'^URL: (.+)$', svn, re.M)

            if not m:
                return None

            base_path = m.group(1)[len(path):] or "/"
            return RepositoryInfo(path=path,
                                  base_path=base_path,
                                  supports_parent_diffs=True)

        self.type = 'hg'

        # We are going to search .hg/hgrc for the default path.
        file_name = os.path.join(local_data, '.hg', 'hgrc')

        if not os.path.exists(file_name):
            return RepositoryInfo(path=local_data, base_path='/',
                                  supports_parent_diffs=True)

        f = open(file_name)
        data = f.read()
        f.close()

        m = re.search(r'^default\s+=\s+(.+)$', data, re.M)

        if not m:
            # Return the local path, if no default value is found.
            return RepositoryInfo(path=local_data, base_path='/',
                                  supports_parent_diffs=True)

        path = m.group(1).strip()

        return RepositoryInfo(path=path, base_path='',
                              supports_parent_diffs=True)

    def diff(self, files):
        """
        Performs a diff across all modified files in a Mercurial repository.
        """
        # We don't support parent diffs with Mercurial yet, so return None
        # for the parent diff.
        if self.type == 'svn':
            return (execute(["hg", "svn", "diff", ]), None)

        return (execute(["hg", "diff"] + files), None)

    def diff_between_revisions(self, revision_range, args, repository_info):
        """
        Performs a diff between 2 revisions of a Mercurial repository.
        """
        if self.type != 'hg':
            raise NotImplementedError

        r1, r2 = revision_range.split(':')
        return execute(["hg", "diff", "-r", r1, "-r", r2])


class GitClient(SCMClient):
    """
    A wrapper around git that fetches repository information and generates
    compatible diffs. This will attempt to generate a diff suitable for the
    remote repository, whether git, SVN or Perforce.
    """
    def get_repository_info(self):
        if not check_install('git --help'):
            return None

        git_dir = execute(["git", "rev-parse", "--git-dir"],
                          ignore_errors=True).strip()

        if git_dir.startswith("fatal:") or not os.path.isdir(git_dir):
            return None

        # post-review in directories other than the top level of
        # of a work-tree would result in broken diffs on the server
        os.chdir(os.path.dirname(git_dir))

        # We know we have something we can work with. Let's find out
        # what it is. We'll try SVN first.
        data = execute(["git", "svn", "info"], ignore_errors=True)

        m = re.search(r'^Repository Root: (.+)$', data, re.M)
        if m:
            path = m.group(1)
            m = re.search(r'^URL: (.+)$', data, re.M)

            if m:
                base_path = m.group(1)[len(path):] or "/"
                self.type = "svn"
                return RepositoryInfo(path=path, base_path=base_path,
                                      supports_parent_diffs=True)
        else:
            # Versions of git-svn before 1.5.4 don't (appear to) support
            # 'git svn info'.  If we fail because of an older git install,
            # here, figure out what version of git is installed and give
            # the user a hint about what to do next.
            version = execute(["git", "svn", "--version"], ignore_errors=True)
            version_parts = re.search('version (\d+)\.(\d+)\.(\d+)',
                                      version)
            svn_remote = execute(["git", "config", "--get",
                                  "svn-remote.svn.url"], ignore_errors=True)

            if (version_parts and
                not self.is_valid_version((int(version_parts.group(1)),
                                           int(version_parts.group(2)),
                                           int(version_parts.group(3))),
                                          (1, 5, 4)) and
                svn_remote):
                die("Your installation of git-svn must be upgraded to " + \
                    "version 1.5.4 or later")

        # Okay, maybe Perforce.
        # TODO

        # Nope, it's git then.
        origin = execute(["git", "remote", "show", "origin"])
        m = re.search(r'URL: (.+)', origin)
        if m:
            url = m.group(1).rstrip('/')
            if url:
                self.type = "git"
                return RepositoryInfo(path=url, base_path='',
                                      supports_parent_diffs=True)

        return None

    def is_valid_version(self, actual, expected):
        """
        Takes two tuples, both in the form:
            (major_version, minor_version, micro_version)
        Returns true if the actual version is greater than or equal to
        the expected version, and false otherwise.
        """
        return (actual[0] > expected[0]) or \
               (actual[0] == expected[0] and actual[1] > expected[1]) or \
               (actual[0] == expected[0] and actual[1] == expected[1] and \
                actual[2] >= expected[2])

    def scan_for_server(self, repository_info):
        # Scan first for dot files, since it's faster and will cover the
        # user's $HOME/.reviewboardrc

        # TODO: Maybe support a server per remote later? Is that useful?
        url = execute(["git", "config", "--get", "reviewboard.url"],
                      ignore_errors=True).strip()
        if url:
            return url

        if self.type == "svn":
            # Try using the reviewboard:url property on the SVN repo, if it
            # exists.
            prop = SVNClient().scan_for_server_property(repository_info)

            if prop:
                return prop

        return None

    def diff(self, args):
        """
        Performs a diff across all modified files in the branch, taking into
        account a parent branch.
        """
        parent_branch = options.parent_branch or "master"

        diff_lines = self.make_diff(parent_branch)

        if parent_branch != "master":
            parent_diff_lines = self.make_diff("master", parent_branch)
        else:
            parent_diff_lines = None

        if options.guess_summary and not options.summary:
            options.summary = execute(["git", "log", "--pretty=format:%s",
                                       "HEAD^.."], ignore_errors=True).strip()

        if options.guess_description and not options.description:
            options.description = execute(
                ["git", "log", "--pretty=format:%s%n%n%b", parent_branch + ".."],
                ignore_errors=True).strip()

        return (diff_lines, parent_diff_lines)

    def make_diff(self, parent_branch, source_branch=""):
        """
        Performs a diff on a particular branch range.
        """
        if self.type == "svn":
            diff_lines = execute(["git", "diff", "--no-color", "--no-prefix",
                                  "-r", "-u", "%s..%s" % (parent_branch,
                                                          source_branch)],
                                 split_lines=True)
            return self.make_svn_diff(parent_branch, diff_lines)
        elif self.type == "git":
            return execute(["git", "diff", "--no-color",
                            parent_branch])

        return None

    def make_svn_diff(self, parent_branch, diff_lines):
        """
        Formats the output of git diff such that it's in a form that
        svn diff would generate. This is needed so the SVNTool in Review
        Board can properly parse this diff.
        """
        rev = execute(["git", "svn", "find-rev", "master"]).strip()

        if not rev:
            return None

        diff_data = ""
        filename = ""
        revision = ""
        newfile = False

        for line in diff_lines:
            if line.startswith("diff "):
                # Grab the filename and then filter this out.
                # This will be in the format of:
                #
                # diff --git a/path/to/file b/path/to/file
                info = line.split(" ")
                diff_data += "Index: %s\n" % info[2]
                diff_data += "=" * 67
                diff_data += "\n"
            elif line.startswith("index "):
                # Filter this out.
                pass
            elif line.strip() == "--- /dev/null":
                # New file
                newfile = True
            elif line.startswith("--- "):
                newfile = False
                diff_data += "--- %s\t(revision %s)\n" % \
                             (line[4:].strip(), rev)
            elif line.startswith("+++ "):
                filename = line[4:].strip()
                if newfile:
                    diff_data += "--- %s\t(revision 0)\n" % filename
                    diff_data += "+++ %s\t(revision 0)\n" % filename
                else:
                    # We already printed the "--- " line.
                    diff_data += "+++ %s\t(working copy)\n" % filename
            else:
                diff_data += line

        return diff_data

    def diff_between_revisions(self, revision_range, args, repository_info):
        pass


def debug(s):
    """
    Prints debugging information if post-review was run with --debug
    """
    if DEBUG or options and options.debug:
        print ">>> %s" % s


def make_tempfile():
    """
    Creates a temporary file and returns the path. The path is stored
    in an array for later cleanup.
    """
    fd, tmpfile = mkstemp()
    os.close(fd)
    tempfiles.append(tmpfile)
    return tmpfile


def check_install(command):
    """
    Try executing an external command and return a boolean indicating whether
    that command is installed or not.  The 'command' argument should be
    something that executes quickly, without hitting the network (for
    instance, 'svn help' or 'git --version').
    """
    try:
        p = subprocess.Popen(command.split(' '),
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             creationflags=0x08000000)
        return True
    except OSError:
        return False


def die(msg=None):
    """
    Cleanly exits the program with an error message. Erases all remaining
    temporary files.
    """
    for tmpfile in tempfiles:
        try:
            os.unlink(tmpfile)
        except:
            pass

    if msg:
        error(msg)

    sys.exit(1)


def walk_parents(path):
    """
    Walks up the tree to the root directory.
    """
    while os.path.splitdrive(path)[1] != os.sep:
        yield path
        path = os.path.dirname(path)


def load_config_file(filename):
    """
    Loads data from a config file.
    """
    config = {
        'TREES': {},
    }

    if os.path.exists(filename):
        try:
            execfile(filename, config)
        except:
            pass

    return config


def tempt_fate(server, tool, changenum, diff_content=None,
               parent_diff_content=None, submit_as=None, review_id = None, branch = None):
    """
    Attempts to create a review request on a Review Board server and upload
    a diff. On success, the review request path is displayed.
    """
    try:
        save_draft = False

        if options.rid:
            rid = options.rid
        elif review_id:
            rid = review_id
        else:
            rid = None

        if rid:
            review_request = server.get_review_request(rid)
        else:
            review_request = server.new_review_request(changenum, submit_as)

        if options.target_groups:
            server.set_review_request_field(review_request, 'target_groups',
                                            options.target_groups)
            save_draft = True

        if options.target_people:
            server.set_review_request_field(review_request, 'target_people',
                                            options.target_people)
            save_draft = True

        if options.summary:
            server.set_review_request_field(review_request, 'summary',
                                            options.summary)
            save_draft = True

        if branch:
            bid = branch
        elif options.branch:
            bid = options.branch
        else:
            bid = None
        if bid:
            server.set_review_request_field(review_request, 'branch',
                                            bid)
            save_draft = True

        if options.bugs_closed:
            server.set_review_request_field(review_request, 'bugs_closed',
                                            options.bugs_closed)
            save_draft = True

        # do not overwrite description if this is an update
        if options.description and not rid:
            server.set_review_request_field(review_request, 'description',
                                            options.description)
            save_draft = True

        if options.testing_done:
            server.set_review_request_field(review_request, 'testing_done',
                                            options.testing_done)
            save_draft = True

        # do not save draft if this is an update
        if save_draft and not rid:
            server.save_draft(review_request)
    except APIError, e:
        rsp, = e.args
        if rsp['err']['code'] == 103: # Not logged in
            server.login()
            tempt_fate(server, tool, changenum, diff_content,
                       parent_diff_content, submit_as, review_id, branch)
            return

        if options.rid:
            die("Error getting review request %s: %s (code %s)\nDetails (not for the faint-hearted): %s" % \
                (options.rid, rsp['err']['msg'], rsp['err']['code'], rsp))
        else:
            die("Error creating review request: %s (code %s)\nDetails (not for the faint-hearted): %s" % \
                (rsp['err']['msg'], rsp['err']['code'], rsp))


    if not server.info.supports_changesets or not options.change_only:
        try:
            server.upload_diff(review_request, diff_content,
                               parent_diff_content)
        except APIError, e:
            rsp, = e.args
            error("Error uploading diff: %s (%s)\nDetails (not for the faint-hearted): %s" % 
                                                    (rsp['err']['msg'],
                                                     rsp['err']['code'],
                                                     rsp))
            debug(rsp)
            die("Your review request still exists, but the diff is not " + 
                "attached.")

    if options.publish:
        server.publish(review_request)

    request_url = 'r/' + str(review_request['id'])
    review_url = urljoin(server.url, request_url)

    if not review_url.startswith('http'):
        review_url = 'http://%s' % review_url

    if not options.gui:
        print "Review request #%s posted." % (review_request['id'],)
        print
        print review_url

    return (review_url, review_request['id'])


def parse_options(args):
    parser = OptionParser(usage="%prog [-pond] [-r review_id] [changenum]",
                          version="%prog " + constants.VERSION)

    parser.add_option("-p", "--publish",
                      dest="publish", action="store_true", default=PUBLISH,
                      help="publish the review request immediately after "
                           "submitting")
    parser.add_option("-r", "--review-request-id",
                      dest="rid", metavar="ID", default=None,
                      help="existing review request ID to update")
    parser.add_option("-o", "--open",
                      dest="open_browser", action="store_true",
                      default=OPEN_BROWSER,
                      help="open a web browser to the review request page")
    parser.add_option("-n", "--output-diff",
                      dest="output_diff_only", action="store_true",
                      default=False,
                      help="outputs a diff to the console and exits. "
                           "Does not post")
    parser.add_option("--server",
                      dest="server", default=REVIEWBOARD_URL,
                      metavar="SERVER",
                      help="specify a different Review Board server "
                           "to use")
    parser.add_option("--diff-only",
                      dest="diff_only", action="store_true", default=False,
                      help="uploads a new diff, but does not update "
                           "info from changelist")
    parser.add_option("--target-groups",
                      dest="target_groups", default=TARGET_GROUPS,
                      help="names of the groups who will perform "
                           "the review")
    parser.add_option("--target-people",
                      dest="target_people", default=TARGET_PEOPLE,
                      help="names of the people who will perform "
                           "the review")
    parser.add_option("--summary",
                      dest="summary", default=None,
                      help="summary of the review ")
    parser.add_option("--description",
                      dest="description", default=None,
                      help="description of the review ")
    parser.add_option("--description-file",
                      dest="description_file", default=None,
                      help="text file containing a description of the review")
    parser.add_option("--guess-summary",
                      dest="guess_summary", action="store_true",
                      default=False,
                      help="guess summary from the latest commit (git only)")
    parser.add_option("--guess-description",
                      dest="guess_description", action="store_true",
                      default=False,
                      help="guess description based on commits on this branch "
                           "(git only)")
    parser.add_option("--testing-done",
                      dest="testing_done", default=None,
                      help="details of testing done ")
    parser.add_option("--testing-done-file",
                      dest="testing_file", default=None,
                      help="text file containing details of testing done ")
    parser.add_option("--branch",
                      dest="branch", default=None,
                      help="affected branch ")
    parser.add_option("--bugs-closed",
                      dest="bugs_closed", default=None,
                      help="list of bugs closed ")
    parser.add_option("--revision-range",
                      dest="revision_range", default=None,
                      help="generate the diff for review based on given "
                           "revision range")
    parser.add_option("--label",
                      dest="label", default=None,
                      help="label (ClearCase Only) ")
    parser.add_option("--submit-as",
                      dest="submit_as", default=SUBMIT_AS, metavar="USERNAME",
                      help="user name to be recorded as the author of the "
                           "review request, instead of the logged in user")
    parser.add_option("--username",
                      dest="username", default=None, metavar="USERNAME",
                      help="user name to be supplied to the reviewboard server")
    parser.add_option("--password",
                      dest="password", default=None, metavar="PASSWORD",
                      help="password to be supplied to the reviewboard server")
    parser.add_option("--change-only",
                      dest="change_only", action="store_true",
                      default=False,
                      help="updates info from changelist, but does "
                           "not upload a new diff (only available if your "
                           "repository supports changesets)")
    parser.add_option("--parent",
                      dest="parent_branch", default=None,
                      metavar="PARENT_BRANCH",
                      help="the parent branch this diff should be against "
                           "(only available if your repository supports "
                           "parent diffs)")
    parser.add_option("--p4-client",
                      dest="p4_client", default=None,
                      help="the Perforce client name that the review is in")
    parser.add_option("--p4-port",
                      dest="p4_port", default=None,
                      help="the Perforce servers IP address that the review is on")
    parser.add_option("--repository-url",
                      dest="repository_url", default=None,
                      help="the url for a repository for creating a diff "
                           "outside of a working copy (currently only supported "
                           "by Subversion).  Requires --revision-range")
    parser.add_option("-d", "--debug",
                      action="store_true", dest="debug", default=DEBUG,
                      help="display debug output")
    parser.add_option("--gui",
                      action="store_true", dest="gui", default=False,
                      help="run post-review interactively")
    parser.add_option("--scmuser",
                      dest="scmuser", default=None,
                      help="overrides the default SCM user")
    parser.add_option("--no-mt",
                      dest="no_mt", action="store_true", default=False,
                      help="disables multithreading (for debugging only)")

    (globals()["options"], args) = parser.parse_args(args)

    if options.description and options.description_file:
        sys.stderr.write("The --description and --description-file options "
                         "are mutually exclusive.\n")
        sys.exit(1)

    if options.description_file:
        if os.path.exists(options.description_file):
            fp = open(options.description_file, "r")
            options.description = fp.read()
            fp.close()
        else:
            sys.stderr.write("The description file %s does not exist.\n" % 
                             options.description_file)
            sys.exit(1)

    if options.testing_done and options.testing_file:
        sys.stderr.write("The --testing-done and --testing-done-file options "
                         "are mutually exclusive.\n")
        sys.exit(1)

    if options.testing_file:
        if os.path.exists(options.testing_file):
            fp = open(options.testing_file, "r")
            options.testing_done = fp.read()
            fp.close()
        else:
            sys.stderr.write("The testing file %s does not exist.\n" % 
                             options.testing_file)
            sys.exit(1)

    if options.repository_url and not options.revision_range:
        sys.stderr.write("The --repository-url option requires the "
                         "--revision-range option.\n")
        sys.exit(1)

    return args

def determine_client():

    repository_info = None
    tool = None

    # Try to find the SCM Client we're going to be working with.
    for tool in (SVNClient(), DtrClient(), CVSClient(), GitClient(), MercurialClient(),
                 PerforceClient(), ClearCaseClient()):
        repository_info = tool.get_repository_info()

        if repository_info:
            break

    if not repository_info:
        if options.repository_url:
            print "No supported repository could be access at the supplied url."
        else:
            print "The current directory does not contain a checkout from a"
            print "supported source code repository."
        sys.exit(1)

    # Verify that options specific to an SCM Client have not been mis-used.
    if options.change_only and not repository_info.supports_changesets:
        sys.stderr.write("The --change-only option is not valid for the "
                         "current SCM client.\n")
        sys.exit(1)

    if options.parent_branch and not repository_info.supports_parent_diffs:
        sys.stderr.write("The --parent option is not valid for the "
                         "current SCM client.\n")
        sys.exit(1)

    if ((options.p4_client or options.p4_port) and \
        not isinstance(tool, PerforceClient)):
        sys.stderr.write("The --p4-client and --p4-port options are not valid "
                         "for the current SCM client.\n")
        sys.exit(1)

    return (repository_info, tool)


#########
## GUI ##
#########


# Events
ReviewPostedEvent, EVT_REVIEW_POSTED = wx.lib.newevent.NewEvent()
ReviewPostingFailedEvent, EVT_REVIEW_POSTING_FAILED = wx.lib.newevent.NewEvent()
SCMErrorEvent, EVT_SCM_FAILED = wx.lib.newevent.NewEvent()
GetLoginDataEvent, EVT_GET_LOGIN_DATA = wx.lib.newevent.NewEvent()
GetLoginDataResponseEvent, EVT_GET_LOGIN_DATA_RESPONSE = wx.lib.newevent.NewEvent()
UpdateAvailableEvent, EVT_UPDATE_AVAILABLE = wx.lib.newevent.NewEvent()


class PostReviewPopupMenu(wx.Menu):
    def __init__(self, parent, itemid):
        wx.Menu.__init__(self)

        self.parent = parent
        self.itemid = itemid

        review = wx.MenuItem(self, wx.NewId(), 'Submit &Review Request')
        self.AppendItem(review)
        self.Bind(wx.EVT_MENU, self.OnPostReview, id = review.GetId())

    def OnPostReview(self, event):
        self.parent.OnSubmitForReview(self.itemid)


class PostReviewListPanel(wx.Panel):
    def __init__(self, title, parent, id, scmclient, cookies):
        wx.Panel.__init__(self, parent, id)
        
        self.list = wx.ListCtrl(self, -1, wx.DefaultPosition, wx.DefaultSize, wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.InsertColumn(0, "Change")
        self.list.InsertColumn(1, "Description")
        self.list.InsertColumn(2, "Branch")
        self.list.InsertColumn(3, "Review #")
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer.Add(self.list, 1, wx.EXPAND)
        self.SetSizer(self.sizer)
        self.SetAutoLayout(1)
        self.sizer.Fit(self)

        self.title = title
        self.scmclient = scmclient
        self.cookies = cookies

        # register context menu
        self.list.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.OnRightClick)

        self.Refresh()
        
    def Refresh(self):
        try:
            changes = self.scmclient.get_open_changes(config.ReadBool(constants.CONFIG_SCM_SHOW_SUBMITTED, constants.DEFAULT_CONFIG_SCM_SHOW_SUBMITTED))
        except:
            if frame is None:
                scm_error(traceback.format_exc())
            else:
                wx.PostEvent(frame, SCMErrorEvent(traceback = traceback.format_exc()))
            return
        #print changes
        self.list.DeleteAllItems()
        #print self.scmclient
        for change in changes:
            #print change.id
            idx = self.list.InsertStringItem(sys.maxint, change.id, -1)
            desc = change.description
            if len(desc) > 60:
                desc = desc[:60] + '[...]'
            self.list.SetStringItem(idx, 1, desc)
            if change.branch:
                self.list.SetStringItem(idx, 2, change.branch)
            reviewid = config.ReadInt(constants.CONFIG_REVIEW_HISTORY_PREFIX % change.id, -1)
            if reviewid > 0:
                self.list.SetStringItem(idx, 3, repr(reviewid))
        #self.list.SetColumnWidth(0, wx.LIST_AUTOSIZE)
        self.list.SetColumnWidth(1, wx.LIST_AUTOSIZE)
        self.list.SetColumnWidth(2, wx.LIST_AUTOSIZE)

    def OnRightClick(self, event):
        self.list.PopupMenu(PostReviewPopupMenu(self, event.GetIndex()), event.GetPoint())

    def OnSubmitForReview(self, id):
        wx.BeginBusyCursor()
        listItem = self.list.GetItem(id)
        changeid = listItem.GetText()
        rid = config.ReadInt(constants.CONFIG_REVIEW_HISTORY_PREFIX % changeid, -1)
        if rid < 0:
            rid = None
        worker = ReviewSubmissionThread(changeid, self.scmclient, self.cookies, rid)
        if options.no_mt:
            worker.run()
        else:
            worker.start()
        
    def GetTitle(self):
        return self.title


class PostReviewWindow(wx.Frame):
    def __init__(self, parent, id, cookies):
        wx.Frame.__init__(self, parent, id, "Post Review - Review Board Client", size = (640, 480))
        
        self.cookies = cookies
        
        icon = wx.Icon('gui/icons/review.ico', wx.BITMAP_TYPE_ICO)
        self.SetIcon(icon)

        self.CreateStatusBar()

        menuBar = wx.MenuBar()

        fileMenu = wx.Menu()
        idSubmit = wx.NewId()
        fileMenu.Append(idSubmit, "&Submit for review", "Submits the selected change for review.")
        idExit = wx.NewId()
        fileMenu.Append(idExit, "E&xit", "Closes this program.")
        menuBar.Append(fileMenu, "&File")

        toolsMenu = wx.Menu()
        idSettings = wx.NewId()
        toolsMenu.Append(idSettings, "&Settings...", "Edits the settings of this program.")
        menuBar.Append(toolsMenu, "&Tools")

        helpMenu = wx.Menu()
        idCheckForUpdates = wx.NewId()
        helpMenu.Append(idCheckForUpdates, "Check for &updates...", "Checks for a newer version of this program.")
        idAbout = wx.NewId()
        helpMenu.Append(idAbout, "&About", "Information about this program.")
        menuBar.Append(helpMenu, "&Help")

        self.SetMenuBar(menuBar)

        toolbar = self.CreateToolBar()
        reviewImage = wx.Image('gui/icons/review.png', wx.BITMAP_TYPE_PNG).ConvertToBitmap() 
        reviewTool = toolbar.AddSimpleTool(idSubmit, bitmap = reviewImage, shortHelpString = "Submit for Review", longHelpString = "Submits the selected change for review.")
        refreshImage = wx.ArtProvider.GetBitmap(wx.ART_REDO, wx.ART_TOOLBAR) 
        idRefresh = wx.NewId()
        refreshTool = toolbar.AddSimpleTool(idRefresh, bitmap = refreshImage, shortHelpString = "Refresh Changes", longHelpString = "Refreshes the list of changes.")
        toolbar.Realize()
        
        self.nb = wx.Notebook(self, -1)

        self.pages = [PostReviewListPanel("DTR (NWDI)", self.nb, -1, DtrClient(), self.cookies)]
        p4_installed = check_install('p4 help')
        if p4_installed:
            self.pages.append(PostReviewListPanel("Perforce", self.nb, -1, PerforceClient(), self.cookies))
        for page in self.pages:
            self.nb.AddPage(page, page.GetTitle())
        self.currentPage = 0
        
        wx.EVT_MENU(self, idAbout, self.OnAbout)
        wx.EVT_MENU(self, idCheckForUpdates, self.OnCheckForUpdates)
        wx.EVT_MENU(self, idExit, self.OnExit)
        wx.EVT_UPDATE_UI(self, idSubmit, self.OnUpdateSubmit)
        wx.EVT_MENU(self, idSubmit, self.OnPostReview)
        wx.EVT_MENU(self, idRefresh, self.OnRefresh)
        wx.EVT_MENU(self, idSettings, self.OnSettings)

        self.Bind(EVT_REVIEW_POSTED, self.OnReviewPosted)
        self.Bind(EVT_REVIEW_POSTING_FAILED, self.OnReviewPostingFailed)
        self.Bind(EVT_GET_LOGIN_DATA, self.OnGetLoginData)
        self.Bind(EVT_UPDATE_AVAILABLE, self.OnUpdateAvailable)
        self.Bind(EVT_SCM_FAILED, self.OnSCMError)
        
        self.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.OnPageChanged)

        # restore window position
        x = config.ReadInt(constants.CONFIG_DIMENSIONS_X, -1)
        y = config.ReadInt(constants.CONFIG_DIMENSIONS_Y, -1)
        width = config.ReadInt(constants.CONFIG_DIMENSIONS_WIDTH, -1)
        height = config.ReadInt(constants.CONFIG_DIMENSIONS_HEIGHT, -1)
        
        if x > -1 and y > -1 and width > 0 and height > 0:
            self.SetRect(wx.Rect(x, y, width, height))
            
        self.Bind(wx.EVT_CLOSE, self.OnClose)

        self.Show(True)
        
        # check for non-domain user
        m = re.search(r'^(d|i|c)\d+$', get_scm_user(config, options).lower(), re.M)
        if not m:
            if wx.MessageBox("You do not seem to be logged on using your domain user, so Post Review cannot figure out your DTR/Perforce user.\n\nWould you like to manually maintain this user now?", "Post Review", wx.YES_NO | wx.CENTER | wx.ICON_EXCLAMATION) == wx.YES:
                EditPreferences(self, config, options)

        if not p4_installed and not config.ReadBool(constants.CONFIG_SCM_IGNORE_P4_MISSING, False):
            dlg = PerforceUnavailableDialog(self, config)
            dlg.ShowModal()

        # check for updates
        worker = CheckForUpdateThread()
        worker.start()

    def OnAbout(self, e):
        dlg = AboutBox(self)
        dlg.ShowModal()

    def OnCheckForUpdates(self, e):
        versioninfo = check_version()
        if versioninfo != None:
            # a newer version is available -> show dialog
            dlg = UpdateAvailableDialog(self, versioninfo[0], versioninfo[1], False)
            dlg.ShowModal()
        else:
            wx.MessageBox("You are running the most recent version of Post Review.", "Post Review", wx.OK | wx.CENTER)

    def OnUpdateAvailable(self, e):
        # a newer version is available -> show dialog
        dlg = UpdateAvailableDialog(self, e.version, e.url, e.unsupported)
        dlg.ShowModal()
        if e.unsupported:
            self.Close(True)

    def OnClose(self, event):
        # store window size and position
        rc = self.GetRect()
        config.WriteInt(constants.CONFIG_DIMENSIONS_X, rc.x)
        config.WriteInt(constants.CONFIG_DIMENSIONS_Y, rc.y)
        config.WriteInt(constants.CONFIG_DIMENSIONS_WIDTH, rc.width)
        config.WriteInt(constants.CONFIG_DIMENSIONS_HEIGHT, rc.height)

        self.Destroy()

    def OnExit(self,e):
        self.Close(True)

    def OnReviewPosted(self, event):
        wx.EndBusyCursor()
        
        # add review ID to list
        updated = False
        for page in self.pages:
            for ii in range(page.list.GetItemCount()):
                if page.list.GetItem(ii).GetText() == event.changeid:
                    page.list.SetStringItem(ii, 3, repr(event.review_id))
                    updated = True
                    break
                if updated:
                    break
        
        # show dialog
        dlg = ReviewPostedDialog(self, event.review_id, event.review_url)
        dlg.ShowModal()

    def OnReviewPostingFailed(self, event):
        wx.EndBusyCursor()
        error("Review submission failed.\n\n%s" % event.traceback)

    def OnGetLoginData(self, event):
        dlg = LoginDialog(self, user = event.user, password = event.password)
        if dlg.ShowModal() == wx.ID_OK:
            result = LoginData(dlg.user, dlg.password, True)
        else:
            result = LoginData(valid = False)
        global login_data
        login_data = result
        uiSemaphore.release()

    def OnUpdateSubmit(self, event):
        page = self.pages[self.currentPage]
        page.list.GetNextItem(-1, state = wx.LIST_STATE_SELECTED)
        event.Enable(page.list.GetSelectedItemCount() > 0)
        
    def OnPageChanged(self, event):
        self.currentPage = event.GetSelection()

    def OnPostReview(self, event):
        page = self.pages[self.currentPage]
        page.OnSubmitForReview(page.list.GetNextItem(-1, state = wx.LIST_STATE_SELECTED))

    def OnRefresh(self, event):
        for page in self.pages:
            page.Refresh()

    def OnSCMError(self, event):
        scm_error(event.traceback)

    def OnSettings(self, event):
        EditPreferences(self, config, options)


def get_login_data(user, password):
    if threading.currentThread() == mainThread:
        dlg = LoginDialog(self, user = event.user, password = event.password)
        if dlg.ShowModal() == wx.ID_OK:
            return LoginData(dlg.user, dlg.password, True)
    else:
        event = GetLoginDataEvent(user = user, password = password)
        event.SetEventObject(threading.currentThread())
        wx.PostEvent(frame, event)
        uiSemaphore.acquire(True)
        global login_data
        if not login_data is None and login_data.valid:
            result = login_data
            login_data = None
            return result

    return None
    

class LoginData(object):
    def __init__(self, user = None, password = None, valid = False):
        self.user = user
        self.password = password
        self.valid = valid


class ReviewSubmissionThread(threading.Thread, wx.EvtHandler):
    def __init__(self, changeid, scmtool, cookies, review_id = None):
        threading.Thread.__init__(self, name = "Review submission thread for change %s" % changeid)
        wx.EvtHandler.__init__(self)
        
        self.changeid = changeid
        self.scmtool = scmtool
        self.cookies = cookies
        self.review_id = review_id

    def run(self):
        try:
            repository_info = self.scmtool.get_repository_info()
            # reset description so P4 CLs do not inherit a previously submitted DTR description
            options.summary = options.description = None
            (review_url, review_id) = post_review(self.scmtool, repository_info, self.cookies, [self.changeid], self.review_id)
            wx.PostEvent(frame, ReviewPostedEvent(changeid = self.changeid, review_id = review_id, review_url = review_url))
        except:
            wx.PostEvent(frame, ReviewPostingFailedEvent(traceback = traceback.format_exc()))

class CheckForUpdateThread(threading.Thread, wx.EvtHandler):
    def __init__(self):
        threading.Thread.__init__(self, name = "Update Checker Thread")
        wx.EvtHandler.__init__(self)

    def run(self):
        versioninfo = check_version()
        if versioninfo != None:
            wx.PostEvent(frame, UpdateAvailableEvent(version = versioninfo[0], url = versioninfo[1], unsupported = versioninfo[2]))


def post_review(tool, repository_info, cookie_file, args, review_id):
        # Try to find a valid Review Board server to use.
        if options.server:
            server_url = options.server
        else:
            server_url = tool.scan_for_server(repository_info)

        if not server_url:
            print "Unable to find a Review Board server for this source code tree."
            sys.exit(1)

        server = ReviewBoardServer(server_url, repository_info, cookie_file)

        if repository_info.supports_changesets:
            if len(args) < 1:
                print "You must include a change set number"
                sys.exit(1)

            changenum = args[0]
        else:
            changenum = None

        if options.revision_range:
            diff = tool.diff_between_revisions(options.revision_range, args,
                                               repository_info)
            parent_diff = None
            branch = None
        elif options.label and isinstance(tool, ClearCaseClient):
            diff, parent_diff = tool.diff_label(options.label)
            branch = None
        else:
            diff, parent_diff, branch = tool.diff(args)

        if options.output_diff_only:
            print diff
            sys.exit(0)

        # Let's begin.
        server.login()

        (review_url, id) = tempt_fate(server, tool, changenum, diff_content=diff,
                                parent_diff_content=parent_diff,
                                submit_as=options.submit_as, review_id = review_id,
                                branch = branch)

        # store review submission history
        if len(args) > 0:
            config.WriteInt(constants.CONFIG_REVIEW_HISTORY_PREFIX % args[0], int(id))

        # Load the review up in the browser if requested to:
        if options.open_browser:
            try:
                import webbrowser
                if 'open_new_tab' in dir(webbrowser):
                    # open_new_tab is only in python 2.5+
                    webbrowser.open_new_tab(review_url)
                elif 'open_new' in dir(webbrowser):
                    webbrowser.open_new(review_url)
                else:
                    os.system('start %s' % review_url)
            except:
                error('Error opening review URL: %s' % review_url)
                
        return (review_url, id)
    

def main(args):
    # enables the creation of UTF-8 based diffs
    try:
        sys.setdefaultencoding("UTF-8")
    except AttributeError:
        # somehow my debugger consistently brings up this error although it works in production
        pass

    mainThread = threading.currentThread()

    if 'USERPROFILE' in os.environ:
        homepath = os.path.join(os.environ["USERPROFILE"], "Local Settings",
                                "Application Data")
    elif 'HOME' in os.environ:
        homepath = os.environ["HOME"]
    else:
        homepath = ''

    # Load the config and cookie files
    globals()['user_config'] = \
        load_config_file(os.path.join(homepath, ".reviewboardrc"))
    cookie_file = os.path.join(homepath, ".post-review-cookies.txt")

    args = parse_options(args)
    
    if options.gui:
        app = wx.PySimpleApp()
        global frame
        frame = PostReviewWindow(None, wx.ID_ANY, cookie_file)
        app.MainLoop()
    else:
        repository_info, tool = determine_client()
        post_review(tool, repository_info, cookie_file, args, None)


def error(msg):
    if options.gui:
        dlg = wx.MessageDialog(frame, msg, caption = 'Post Review - Review Board Client', style = wx.OK | wx.ICON_ERROR | wx.CENTRE)
        dlg.ShowModal()
    else:
        print msg


def scm_error(traceback):
    error("Could not talk to the SCM.\n\n%s" % traceback)


def check_version():
    try:
        proxy_support = urllib2.ProxyHandler({})
        opener = urllib2.build_opener(proxy_support)
        opener.addheaders = [('User-agent', 'post-review/' + constants.VERSION)]

        version = opener.open(constants.VERSIONCHECK_URL).read()
        m = re.search('^(\d+)\t([^\t]+)\t(.+)$', version)

        if m and m.group(1) > constants.VERSION_NUMBER:
            unsupported = False
            try:
                last_supported = opener.open(constants.VERSIONCHECK_SUPPORTED_URL).read()
                m2 = re.search('^(\d+)\r?\n?$', last_supported)
                if m2 and m2.group(1) > constants.VERSION_NUMBER:
                    unsupported = True
            except urllib2.URLError:
                pass
            return (m.group(2), m.group(3).rstrip("\r\n"), unsupported)
            
    except urllib2.URLError:
        pass
    return None


if __name__ == "__main__":
    main(sys.argv[1:])
