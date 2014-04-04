# -*- coding: utf-8 -*-

""" S3 Synchronization: Peer Repository API Adapter

    @copyright: 2011-14 (c) Sahana Software Foundation
    @license: MIT

    Permission is hereby granted, free of charge, to any person
    obtaining a copy of this software and associated documentation
    files (the "Software"), to deal in the Software without
    restriction, including without limitation the rights to use,
    copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following
    conditions:

    The above copyright notice and this permission notice shall be
    included in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
    EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
    OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
    NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
    HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
    WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
    FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
    OTHER DEALINGS IN THE SOFTWARE.
"""

import sys
import urllib, urllib2

from gluon import *
from gluon.storage import Storage

from ..s3sync import S3SyncRepository

# =============================================================================
class S3SyncCiviCRM(S3SyncRepository):
    """
        CiviCRM REST-API connector

        @status: experimental
    """

    # Resource map
    RESOURCE = {
        "pr_person": {
                      "q": "civicrm/contact",
                      "contact_type": "Individual"
                     },
    }

    # -------------------------------------------------------------------------
    def register(self):
        """ Register at the repository """

        # CiviCRM does not support via-web peer registration
        return True

    # -------------------------------------------------------------------------
    def login(self):
        """ Login to the repository """

        _debug("S3SyncCiviCRM.login()")

        request = {
            "q": "civicrm/login",
            "name": self.username,
            "pass": self.password,
        }
        response, error = self.send(**request)

        if error:
            _debug("S3SyncCiviCRM.login FAILURE: %s" % error)
            return error

        api_key = response.findall("//api_key")
        if len(api_key):
            self.api_key = api_key[0].text
        else:
            error = "No API Key returned by CiviCRM"
            _debug("S3SyncCiviCRM.login FAILURE: %s" % error)
            return error
        PHPSESSID = response.findall("//PHPSESSID")
        if len(PHPSESSID):
            self.PHPSESSID = PHPSESSID[0].text
        else:
            error = "No PHPSESSID returned by CiviCRM"
            _debug("S3SyncCiviCRM.login FAILURE: %s" % error)
            return error

        _debug("S3SyncCiviCRM.login SUCCESS")
        return None

    # -------------------------------------------------------------------------
    def pull(self, task, onconflict=None):
        """
            Pull updates from this repository

            @param task: the task Row
            @param onconflict: synchronization conflict resolver
        """

        xml = current.xml
        log = self.log
        resource_name = task.resource_name

        _debug("S3SyncCiviCRM.pull(%s, %s)" % (self.url, resource_name))

        mtime = None
        message = ""
        remote = False

        # Construct the request
        if resource_name not in self.RESOURCE:
            result = log.FATAL
            message = "Resource type %s currently not supported for CiviCRM synchronization" % \
                      resource_name
            output = xml.json_message(False, 400, message)
        else:
            args = Storage(self.RESOURCE[resource_name])
            args["q"] += "/get"

            tree, error = self.send(method="GET", **args)
            if error:

                result = log.FATAL
                remote = True
                message = error
                output = xml.json_message(False, 400, error)

            elif len(tree.getroot()):

                result = log.SUCCESS
                remote = False

                # Get import strategy and update policy
                strategy = task.strategy
                update_policy = task.update_policy
                conflict_policy = task.conflict_policy

                # Import stylesheet
                folder = current.request.folder
                import os
                stylesheet = os.path.join(folder,
                                          "static",
                                          "formats",
                                          "ccrm",
                                          "import.xsl")

                # Host name of the peer,
                # used by the import stylesheet
                import urlparse
                hostname = urlparse.urlsplit(self.url).hostname

                # Import the data
                resource = current.s3db.resource(resource_name)
                if onconflict:
                    onconflict_callback = lambda item: onconflict(item,
                                                                  self,
                                                                  resource)
                else:
                    onconflict_callback = None
                count = 0
                success = True
                try:
                    success = resource.import_xml(tree,
                                               stylesheet=stylesheet,
                                               ignore_errors=True,
                                               strategy=strategy,
                                               update_policy=update_policy,
                                               conflict_policy=conflict_policy,
                                               last_sync=task.last_pull,
                                               onconflict=onconflict_callback,
                                               site=hostname)
                    count = resource.import_count
                except IOError, e:
                    result = log.FATAL
                    message = "%s" % e
                    output = xml.json_message(False, 400, message)
                mtime = resource.mtime

                # Log all validation errors
                if resource.error_tree is not None:
                    result = log.WARNING
                    message = "%s" % resource.error
                    for element in resource.error_tree.findall("resource"):
                        for field in element.findall("data[@error]"):
                            error_msg = field.get("error", None)
                            if error_msg:
                                msg = "(UID: %s) %s.%s=%s: %s" % \
                                        (element.get("uuid", None),
                                         element.get("name", None),
                                         field.get("field", None),
                                         field.get("value", field.text),
                                         field.get("error", None))
                                message = "%s, %s" % (message, msg)

                # Check for failure
                if not success:
                    result = log.FATAL
                    if not message:
                        message = "%s" % resource.error
                    output = xml.json_message(False, 400, message)
                    mtime = None

                # ...or report success
                elif not message:
                    message = "data imported successfully (%s records)" % count
                    output = None

            else:
                # No data received from peer
                result = log.ERROR
                remote = True
                message = "no data received from peer"
                output = None

        # Log the operation
        log.write(repository_id=self.id,
                  resource_name=resource_name,
                  transmission=log.OUT,
                  mode=log.PULL,
                  action=None,
                  remote=remote,
                  result=result,
                  message=message)

        _debug("S3SyncCiviCRM.pull import %s: %s" % (result, message))
        return (output, mtime)

    # -------------------------------------------------------------------------
    def push(self, task):
        """
            Push data for a task

            @param task: the task Row
        """

        xml = current.xml
        log = self.log
        resource_name = task.resource_name

        _debug("S3SyncCiviCRM.push(%s, %s)" % (self.url, resource_name))

        result = log.FATAL
        remote = False
        message = "Push to CiviCRM currently not supported"
        output = xml.json_message(False, 400, message)

        # Log the operation
        log.write(repository_id=self.id,
                  resource_name=resource_name,
                  transmission=log.OUT,
                  mode=log.PUSH,
                  action=None,
                  remote=remote,
                  result=result,
                  message=message)

        _debug("S3SyncCiviCRM.push export %s: %s" % (result, message))
        return(output, None)

    # -------------------------------------------------------------------------
    def send(self, method="GET", **args):

        config = self.get_config()

        # Authentication
        args = Storage(args)
        if hasattr(self, "PHPSESSID") and self.PHPSESSID:
            args["PHPSESSID"] = self.PHPSESSID
        if hasattr(self, "api_key") and self.api_key:
            args["api_key"] = self.api_key
        if hasattr(self, "site_key") and self.site_key:
            args["key"] = self.site_key

        # Create the request
        url = self.url + "?" + urllib.urlencode(args)
        req = urllib2.Request(url=url)
        handlers = []

        # Proxy handling
        proxy = self.proxy or config.proxy or None
        if proxy:
            _debug("using proxy=%s" % proxy)
            proxy_handler = urllib2.ProxyHandler({protocol: proxy})
            handlers.append(proxy_handler)

        # Install all handlers
        if handlers:
            opener = urllib2.build_opener(*handlers)
            urllib2.install_opener(opener)

        # Execute the request
        response = None
        message = None

        try:
            if method == "POST":
                f = urllib2.urlopen(req, data="")
            else:
                f = urllib2.urlopen(req)
        except urllib2.HTTPError, e:
            message = "HTTP %s: %s" % (e.code, e.reason)
        else:
            # Parse the response
            tree = current.xml.parse(f)
            root = tree.getroot()
            #print current.xml.tostring(tree, pretty_print=True)
            is_error = root.xpath("//ResultSet[1]/Result[1]/is_error")
            if len(is_error) and int(is_error[0].text):
                error = root.xpath("//ResultSet[1]/Result[1]/error_message")
                if len(error):
                    message = error[0].text
                else:
                    message = "Unknown error"
            else:
                response = tree

        return response, message

# End =========================================================================
