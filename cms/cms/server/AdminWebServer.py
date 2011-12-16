#!/usr/bin/python
# -*- coding: utf-8 -*-

# Programming contest management system
# Copyright © 2010-2011 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2011 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2011 Matteo Boscariol <boscarim@hotmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Web server for administration of contests.

"""

import os
import time

import simplejson
import tornado.web
import tornado.locale

from cms.async.AsyncLibrary import logger, rpc_callback
from cms.async.WebAsyncLibrary import WebService
from cms.async import ServiceCoord, get_service_shards, get_service_address

from cms.db.SQLAlchemyAll import Session, \
     Contest, User, Announcement, Question, Message, Submission, File, Task, \
     Attachment, Manager, Testcase, SubmissionFormatElement

import cms.util.WebConfig as WebConfig
from cms.server.Utils import file_handler_gen
from cms.service.FileStorage import FileCacher
from cms import Config


class BaseHandler(tornado.web.RequestHandler):
    """Base RequestHandler for this application.

    All the RequestHandler classes in this application should be a
    child of this class.

    """

    def retrieve_contest(self, contest_id):
        """Retrieve the contest with the specified id.

        Raise tornado.web.HTTPError(404) if the contest doesn't exist.

        """
        self.contest = Contest.get_from_id(contest_id, self.sql_session)
        if self.contest is None:
            raise tornado.web.HTTPError(404)

    def prepare(self):
        """This method is executed at the beginning of each request.

        """
        # Attempt to update the contest and all its references
        # If this fails, the request terminates.
        self.set_header("Cache-Control", "no-cache, must-revalidate")

        self.sql_session = Session()
        self.sql_session.expire_all()
        self.contest = None

        localization_dir = os.path.join(os.path.dirname(__file__), "mo")
        if os.path.exists(localization_dir):
            tornado.locale.load_gettext_translations(localization_dir, "cms")

    def render_params(self):
        """Return the default render params used by almost all handlers.

        return (dict): default render params

        """
        params = {}
        params["timestamp"] = int(time.time())
        params["contest"] = self.contest
        if self.contest is not None:
            params["phase"] = self.contest.phase(params["timestamp"])
            # Keep "== None" in filter arguments
            params["unanswered"] = self.sql_session.query(Question)\
                                    .join(User)\
                                    .filter(User.contest_id ==
                                            self.contest.id)\
                                    .filter(Question.reply_timestamp == None)\
                                    .count()
        params["contest_list"] = self.sql_session.query(Contest).all()
        params["cookie"] = str(self.cookies)
        return params

    def finish(self, *args, **kwds):
        """ Finish this response, ending the HTTP request.

        We override this method in order to properly close the database.

        """
        logger.debug("Closing SQL connection.")
        self.sql_session.close()
        tornado.web.RequestHandler.finish(self, *args, **kwds)

    def get_non_negative_int(self, argument_name, default, allow_empty=True):
        """ Get a non-negative integer from the arguments.

        Use default if the argument is missing; If allow_empty=False,
        Empty values such as "" and None are not permitted.

        Raise ValueError if the argument can't be converted into a non-negative
        integer.

        """
        argument = self.get_argument(argument_name, repr(default))
        if allow_empty and \
               (argument is None or argument == "" or argument == "None"):
            return None
        try:
            argument = int(argument)
        except:
            raise ValueError("%s: can't cast %s to int." %
                             (argument_name, argument))
        if argument < 0:
            raise ValueError("%s is negative." % argument_name)
        return argument

FileHandler = file_handler_gen(BaseHandler)


class AdminWebServer(WebService):
    """Service that runs the web server serving the managers.

    """

    def __init__(self, shard):
        logger.initialize(ServiceCoord("AdminWebServer", shard))
        logger.debug("AdminWebServer.__init__")

        # A list of pending notifications.
        self.notifications = []

        parameters = WebConfig.admin_parameters
        parameters["template_path"] = os.path.join(os.path.dirname(__file__),
                                                   "templates", "admin")
        parameters["static_path"] = os.path.join(os.path.dirname(__file__),
                                                 "static")
        WebService.__init__(self,
                            Config.admin_listen_port,
                            handlers,
                            parameters,
                            shard=shard)
        self.FC = FileCacher(self)
        self.ES = self.connect_to(ServiceCoord("EvaluationService", 0))
        self.RS = []
        for i in xrange(get_service_shards("ResourceService")):
            self.RS.append(self.connect_to(ServiceCoord("ResourceService", i)))
        self.logservice = self.connect_to(ServiceCoord("LogService", 0))

    def authorized_rpc(self, service, method, arguments):
        """Used by WebService to check if the browser can call a
        certain RPC method.

        service (ServiceCoord): the service called by the browser.
        method (string): the name of the method called.
        arguments (dict): the arguments of the call.
        return (bool): True if ok, False if not authorized.

        """
        if service == ServiceCoord("EvaluationService", 0):
            if method == "submissions_status":
                return True
            elif method == "queue_status":
                return True
            elif method == "workers_status":
                return True

        if service == ServiceCoord("LogService", 0):
            if method == "last_messages":
                return True

        if service.name == "ResourceService":
            if method == "get_resources":
                return True
            elif method == "kill_service":
                return True

        # Default fallback: don't authorize.
        return False

    def add_notification(self, timestamp, subject, text):
        """Store a new notification to send at the first
        opportunity (i.e., at the first request for db notifications).

        timestamp (int): the time of the notification.
        subject (string): subject of the notification.
        text (string): body of the notification.

        """
        self.notifications.append((timestamp, subject, text))


class MainHandler(BaseHandler):
    """Home page handler, with queue and workers statuses.

    """

    def get(self, contest_id=None):
        if contest_id is not None:
            self.retrieve_contest(contest_id)

        r_params = self.render_params()
        self.render("welcome.html", **r_params)


def SimpleContestHandler(page):
    class Cls(BaseHandler):
        def get(self, contest_id):
            self.retrieve_contest(contest_id)
            r_params = self.render_params()
            self.render(page, **r_params)
    return Cls


class ResourcesHandler(BaseHandler):
    def get(self, contest_id=None):
        if contest_id is not None:
            self.retrieve_contest(contest_id)

        r_params = self.render_params()
        r_params["resource_shards"] = get_service_shards("ResourceService")
        r_params["resource_addresses"] = {}
        for i in xrange(r_params["resource_shards"]):
            r_params["resource_addresses"][i] = get_service_address(
                ServiceCoord("ResourceService", i)).ip

        self.render("resources.html", **r_params)


class AddContestHandler(BaseHandler):
    """Adds a new contest.

    """
    def get(self):
        r_params = self.render_params()
        self.render("add_contest.html", **r_params)

    def post(self):

        name = self.get_argument("name", "")
        if name == "":
            self.write("No contest name specified")
            return

        description = self.get_argument("description", None)

        try:
            token_initial = self.get_non_negative_int(
                "token_initial",
                0,
                allow_empty=False)
            token_max = self.get_non_negative_int(
                "token_max",
                None)
            token_total = self.get_non_negative_int(
                "token_total",
                None)
            token_min_interval = self.get_non_negative_int(
                "token_min_interval",
                None)
            token_gen_time = self.get_non_negative_int(
                "token_gen_time",
                None)
            token_gen_number = self.get_non_negative_int(
                "token_gen_number",
                None)
        except Exception as e:
            self.write("Invalid token field(s). %r" % e)
            return

        try:
            start = time.mktime(time.strptime(self.get_argument("start", ""),
                                              "%d/%m/%Y %H:%M:%S"))
            stop = time.mktime(time.strptime(self.get_argument("end", ""),
                                             "%d/%m/%Y %H:%M:%S"))
        except Exception as e:
            self.write("Invalid date(s). %r" % e)
            return

        if start > stop:
            self.write("Contest ends before it starts")
            return

        c = Contest(name, description, [], [], token_initial,
            token_max, token_total, token_min_interval,
            token_gen_time, token_gen_number, start, stop)

        self.sql_session.add(c)
        self.sql_session.commit()
        self.write(str(c.id))

class AddStatementHandler(BaseHandler):
    """Add a statement to a task.

    """
    def get(self, task_id):
        task = Task.get_from_id(task_id, self.sql_session)
        if task is None:
            raise tornado.web.HTTPError(404)
        self.contest = task.contest
        r_params = self.render_params()
        r_params["task"] = task
        self.render("add_statement.html", **r_params)

    @tornado.web.asynchronous
    def post(self, task_id):
        self.task = Task.get_from_id(task_id, self.sql_session)
        if self.task is None:
            raise tornado.web.HTTPError(404)
        self.contest = self.task.contest
        self.statement = self.request.files["statement"][0]
        if not self.statement["filename"].endswith(".pdf"):
            self.application.service.add_notification(time.time(),
                "Invalid task statement",
                "The task statement must be a .pdf file.")
            self.redirect("/add_statement/%s" % self.task.id)
            return
        if self.application.service.FS.put_file(
            callback=AddStatementHandler.storage_callback,
            plus=self.statement["filename"],
            binary_data=self.statement["body"],
            description="Task statement for %s" % self.task.name,
            bind_obj=self) == False:
            self.storage_callback(None, None, error="Connection failed.")

    @rpc_callback
    def storage_callback(self, data, plus, error=None):
        if error is None:
            self.task.statement = data
            self.sql_session.commit()
            self.redirect("/task/%s" % self.task.id)
        else:
            self.application.service.add_notification(time.time(),
                "Task statement storage failed",
                error)
            self.redirect("/add_statement/%s" % self.task.id)

class AddAttachmentHandler(BaseHandler):
    """Add an attachment to a task.

    """
    def get(self, task_id):
        task = Task.get_from_id(task_id, self.sql_session)
        if task is None:
            raise tornado.web.HTTPError(404)
        self.contest = task.contest
        r_params = self.render_params()
        r_params["task"] = task
        self.render("add_attachment.html", **r_params)

    @tornado.web.asynchronous
    def post(self, task_id):
        self.task = Task.get_from_id(task_id, self.sql_session)
        if self.task is None:
            raise tornado.web.HTTPError(404)
        self.contest = self.task.contest
        self.attachment = self.request.files["attachment"][0]
        if self.application.service.FS.put_file(
            callback=AddAttachmentHandler.storage_callback,
            plus=self.attachment["filename"],
            binary_data=self.attachment["body"],
            description="Task attachment for %s" % self.task.name,
            bind_obj=self) == False:
            self.storage_callback(None, None, error="Connection failed.")

    @rpc_callback
    def storage_callback(self, data, plus, error=None):
        if error is None:
            self.sql_session.add(Attachment(
                data,
                self.attachment["filename"],
                self.task))
            self.sql_session.commit()
            self.redirect("/task/%s" % self.task.id)
        else:
            self.application.service.add_notification(time.time(),
                "Attachment storage failed",
                error)
            self.redirect("/add_statement/%s" % self.task.id)

class DeleteAttachmentHandler(BaseHandler):
    """Delete an attachment.

    """
    def get(self, attachment_id):
        attachment = Attachment.get_from_id(attachment_id, self.sql_session)
        if attachment is None:
            raise tornado.web.HTTPError(404)
        task = attachment.task
        self.sql_session.delete(attachment)
        self.sql_session.commit()
        # TODO: Remove the attachment from FS.
        self.redirect("/task/%s" % task.id)

class AddManagerHandler(BaseHandler):
    """Add a manager to a task.

    """
    def get(self, task_id):
        task = Task.get_from_id(task_id, self.sql_session)
        if task is None:
            raise tornado.web.HTTPError(404)
        self.contest = task.contest
        r_params = self.render_params()
        r_params["task"] = task
        self.render("add_manager.html", **r_params)

    @tornado.web.asynchronous
    def post(self, task_id):
        self.task = Task.get_from_id(task_id, self.sql_session)
        if self.task is None:
            raise tornado.web.HTTPError(404)
        self.contest = self.task.contest
        self.manager = self.request.files["manager"][0]
        if self.application.service.FS.put_file(
            callback=AddManagerHandler.storage_callback,
            plus=self.manager["filename"],
            binary_data=self.manager["body"],
            description="Task manager for %s" % self.task.name,
            bind_obj=self) == False:
            self.storage_callback(None, None, error="Connection failed.")

    @rpc_callback
    def storage_callback(self, data, plus, error=None):
        if error is None:
            self.sql_session.add(Manager(
                data,
                self.manager["filename"],
                self.task))
            self.sql_session.commit()
            self.redirect("/task/%s" % self.task.id)
        else:
            self.application.service.add_notification(time.time(),
                "Manager storage failed",
                error)
            self.redirect("/add_manager/%s" % self.task.id)

class DeleteManagerHandler(BaseHandler):
    """Delete a manager.

    """
    def get(self, manager_id):
        manager = Manager.get_from_id(manager_id, self.sql_session)
        if manager is None:
            raise tornado.web.HTTPError(404)
        task = manager.task
        self.sql_session.delete(manager)
        self.sql_session.commit()
        # TODO: Remove the manager from FS.
        self.redirect("/task/%s" % task.id)

class AddTestcaseHandler(BaseHandler):
    """Add a testcase to a task.

    """
    def get(self, task_id):
        task = Task.get_from_id(task_id, self.sql_session)
        if task is None:
            raise tornado.web.HTTPError(404)
        self.contest = task.contest
        r_params = self.render_params()
        r_params["task"] = task
        self.render("add_testcase.html", **r_params)

    @tornado.web.asynchronous
    def post(self, task_id):
        self.task = Task.get_from_id(task_id, self.sql_session)
        if self.task is None:
            raise tornado.web.HTTPError(404)
        self.contest = self.task.contest
        self.input = self.request.files["input"][0]
        self.output = self.request.files["output"][0]
        self.public = self.get_argument("public", None) is not None

        self.successful_calls = {};

        if self.application.service.FS.put_file(
            callback=AddTestcaseHandler.storage_callback,
            plus="input",
            binary_data=self.input["body"],
            description="Testcase input for task %s" % self.task.name,
            bind_obj=self) == False:
            self.storage_callback(None, None, error="Connection failed.")

        if self.application.service.FS.put_file(
            callback=AddTestcaseHandler.storage_callback,
            plus="output",
            binary_data=self.output["body"],
            description="Testcase output for task %s" % self.task.name,
            bind_obj=self) == False:
            self.storage_callback(None, None, error="Connection failed.")

    @rpc_callback
    def storage_callback(self, data, plus, error=None):
        if error is None:
            self.successful_calls[plus] = data;
            if len(self.successful_calls) >= 2:
                self.sql_session.add(Testcase(
                    self.successful_calls["input"],
                    self.successful_calls["output"],
                    len(self.task.testcases),
                    self.public,
                    self.task))
                self.sql_session.commit()
                self.redirect("/task/%s" % self.task.id)
        else:
            self.application.service.add_notification(time.time(),
                "Testcase storage failed",
                error)
            self.redirect("/add_testcase/%s" % self.task.id)

class DeleteTestcaseHandler(BaseHandler):
    """Delete a testcase.

    """
    def get(self, testcase_id):
        testcase = Testcase.get_from_id(testcase_id, self.sql_session)
        if testcase is None:
            raise tornado.web.HTTPError(404)
        task = testcase.task
        self.sql_session.delete(testcase)
        self.sql_session.commit()
        # TODO: Remove the testcase from FS.
        self.redirect("/task/%s" % task.id)

class TaskViewHandler(BaseHandler):
    """Task handler, with a POST method to edit the task.

    """
    def get(self, task_id):
        task = Task.get_from_id(task_id, self.sql_session)
        if task is None:
            raise tornado.web.HTTPError(404)
        self.contest = task.contest
        r_params = self.render_params()
        r_params["task"] = task
        r_params["submissions"] = self.sql_session.query(Submission)\
                                  .join(Task).filter(Task.id == task_id)\
                                  .order_by(Submission.timestamp.desc())
        self.render("task.html", **r_params)

    @tornado.web.asynchronous
    def post(self, task_id):
        self.task = Task.get_from_id(task_id, self.sql_session)
        if self.task is None:
            raise tornado.web.HTTPError(404)
        self.task.name = self.get_argument("name", self.task.name)
        self.task.title = self.get_argument("title", self.task.title)

        time_limit = self.get_argument("time_limit",
                                       repr(self.task.time_limit))
        try:
            time_limit = float(time_limit)
            if time_limit < 0 or time_limit > "+inf":
                raise TypeError("Time limit out of range.")
        except TypeError as e:
            self.write("Invalid time limit.")
            self.finish()
            return
        self.task.time_limit = time_limit

        try:
            self.task.memory_limit = self.get_non_negative_int(
                "memory_limit",
                self.task.memory_limit,
                allow_empty=False)
            if self.task.memory_limit == 0:
                raise ValueError("Memory limit is 0.")
            self.task.token_initial = self.get_non_negative_int(
                "token_initial",
                self.task.token_initial,
                allow_empty=False)
            self.task.token_max = self.get_non_negative_int(
                "token_max",
                self.task.token_max)
            self.task.token_total = self.get_non_negative_int(
                "token_total",
                self.task.token_total)
            self.task.token_min_interval = self.get_non_negative_int(
                "token_min_interval",
                self.task.token_min_interval)
            self.task.token_gen_time = self.get_non_negative_int(
                "token_gen_time",
                self.task.token_gen_time)
            self.task.token_gen_number = self.get_non_negative_int(
                "token_gen_number",
                self.task.token_gen_number)
        except ValueError as e:
            self.write("Invalid fields. %r" % e)
            self.finish()
            return

        for testcase in self.task.testcases:
            testcase.public = self.get_argument(
                "testcase_%s_public" % testcase.num,
                False) != False

        self.task.task_type = self.get_argument("task_type", "")

        if self.task.task_type == "TaskTypeBatch":
            batch_evaluation = self.get_argument("Batch_evaluation","")
            if batch_evaluation not in ["diff", "comp", "grad"]:
                self.application.service.add_notification(time.time(),
                "Invalid field",
                "Output evaluation not recognized.")
                self.redirect("/task/%s" % task_id)
            batch_use_files = self.get_argument("Batch_use_files", None)

            if batch_use_files is not None:
                batch_use_files = "file"
            else:
                batch_use_files = "nofile"
            task_type_parameters = [batch_evaluation, batch_use_files]

        elif self.task.task_type == "TaskTypeOutputOnly":
            oo_evaluation = self.get_argument("OutputOnly_evaluation","")
            if oo_evaluation not in ["diff", "comp"]:
                self.application.service.add_notification(time.time(),
                "Invalid field",
                "Output evaluation not recognized.")
                self.redirect("/task/%s" % task_id)

            task_type_parameters = [oo_evaluation]

        else:
            self.application.service.add_notification(time.time(),
                "Invalid field",
                "Task type not recognized.")
            self.redirect("/add_task/%s" % contest_id)

        self.task.task_type_parameters = simplejson.dumps(task_type_parameters)

        self.task.score_type = self.get_argument("score_type", "")

        self.task.score_parameters = self.get_argument("score_parameters","")

        submission_format = self.get_argument("submission_format","")
        if submission_format not in ["", "[]"] \
            and submission_format != simplejson.dumps(
                [x.filename for x in self.task.submission_format]
                ):
            try:
                format_list = simplejson.loads(submission_format)
                for element in self.task.submission_format:
                    self.sql_session.delete(element)
                del self.task.submission_format[:]
                for element in format_list:
                    self.sql_session.add(SubmissionFormatElement(str(element), self.task))
            except Exception as e:
                self.sql_session.rollback()
                logger.info(e)
                self.application.service.add_notification(time.time(),
                "Invalid field",
                "Submission format not recognized.")
                self.redirect("/task/%s" % task_id)
                return

        self.sql_session.commit()
        self.redirect("/task/%s" % self.task.id)


class TaskStatementViewHandler(FileHandler):
    """Shows the statement file of a task in the contest.

    """
    @tornado.web.asynchronous
    def get(self, task_id):
        r_params = self.render_params()
        try:
            task = Task.get_from_id(task_id, self.sql_session)
        except IndexError:
            self.write("Task %s not found." % task_id)

        self.fetch(task.statement, "application/pdf", "%s.pdf" % task.name)

class AddTaskHandler(SimpleContestHandler("add_task.html")):
    def post(self, contest_id):
        self.retrieve_contest(contest_id)

        name = self.get_argument("name","")
        title = self.get_argument("title", "")
        time_limit = self.get_argument("time_limit", "")
        memory_limit = self.get_argument("memory_limit","")
        task_type = self.get_argument("task_type", "")

        if task_type == "TaskTypeBatch":
            batch_evaluation = self.get_argument("Batch_evaluation","")
            if batch_evaluation not in ["diff", "comp", "grad"]:
                self.application.service.add_notification(time.time(),
                "Invalid field",
                "Output evaluation not recognized.")
                self.redirect("/add_task/%s" % contest_id)
            batch_use_files = self.get_argument("Batch_use_files", None)

            if batch_use_files is not None:
                batch_use_files = "file"
            else:
                batch_use_files = "nofile"
            task_type_parameters = [batch_evaluation, batch_use_files]

        elif task_type == "TaskTypeOutputOnly":
            oo_evaluation = self.get_argument("OutputOnly_evaluation","")
            if oo_evaluation not in ["diff", "comp"]:
                self.application.service.add_notification(time.time(),
                "Invalid field",
                "Output evaluation not recognized.")
                self.redirect("/add_task/%s" % contest_id)

            task_type_parameters = [oo_evaluation]

        else:
            self.application.service.add_notification(time.time(),
                "Invalid field",
                "Task type not recognized.")
            self.redirect("/add_task/%s" % contest_id)

        task_type_parameters = simplejson.dumps(task_type_parameters)

        submission_format_choice = self.get_argument("submission_format", "")

        if submission_format_choice == "simple":
            submission_format = [SubmissionFormatElement("%s.%%l" % name)]
        elif submission_format_choice == "other":
            submission_format = self.get_argument("submission_format_other", "")
            if submission_format not in ["", "[]"]:
                try:
                    format_list = simplejson.loads(submission_format)
                    submission_format = []
                    for element in format_list:
                        submission_format.append(SubmissionFormatElement(str(element)))
                except Exception as e:
                    self.sql_session.rollback()
                    logger.info(e)
                    self.application.service.add_notification(time.time(),
                    "Invalid field",
                    "Submission format not recognized.")
                    self.redirect("/task/%s" % task_id)
                    return
        else:
            self.application.service.add_notification(time.time(),
                "Invalid field",
                "Submission format not recognized.")
            self.redirect("/add_task/%s" % contest_id)

        score_type = self.get_argument("score_type", "")
        score_parameters = self.get_argument("score_parameters","")

        attachments = {}
        statement = ""
        managers = {}
        testcases = []

        token_initial = self.get_non_negative_int(
            "token_initial",
            None,
            allow_empty=False)
        token_max = self.get_non_negative_int(
            "token_max",
            None)
        token_total = self.get_non_negative_int(
            "token_total",
            None)
        token_min_interval = self.get_non_negative_int(
            "token_min_interval",
            None)
        token_gen_time = self.get_non_negative_int(
            "token_gen_time",
            None)
        token_gen_number = self.get_non_negative_int(
            "token_gen_number",
            None)
        task = Task(name, title, attachments, statement,
                 time_limit, memory_limit,
                 task_type, task_type_parameters, submission_format, managers,
                 score_type, score_parameters, testcases,
                 token_initial, token_max, token_total,
                 token_min_interval, token_gen_time, token_gen_number,
                 contest=self.contest, num=len(self.contest.tasks))
        self.sql_session.add(task)
        self.sql_session.commit()
        self.redirect("/task/%d" % task.id)

class EditContestHandler(BaseHandler):
    """Called when managers edit the information of a contest.

    """
    def post(self, contest_id):
        self.retrieve_contest(contest_id)

        name = self.get_argument("name", "")
        if name == "":
            self.application.service.add_notification(int(time.time()),
                "No contest name specified", "")
            self.redirect("/contest/%s" % contest_id)
            return

        description = self.get_argument("description", None)

        try:
            token_initial = self.get_non_negative_int(
                "token_initial",
                self.contest.token_initial,
                allow_empty=False)
            token_max = self.get_non_negative_int(
                "token_max",
                self.contest.token_max,
                allow_empty=True)
            token_total = self.get_non_negative_int(
                "token_total",
                self.contest.token_total)
            token_min_interval = self.get_non_negative_int(
                "token_min_interval",
                self.contest.token_min_interval)
            token_gen_time = self.get_non_negative_int(
                "token_gen_time",
                self.contest.token_gen_time)
            token_gen_number = self.get_non_negative_int(
                "token_gen_number",
                self.contest.token_gen_number)
        except Exception as e:
            self.application.service.add_notification(int(time.time()),
                "Invalid token field(s).", repr(e))
            self.redirect("/contest/%s" % contest_id)
            return

        try:
            start = time.mktime(time.strptime(self.get_argument("start", ""),
                                              "%d/%m/%Y %H:%M:%S"))
            stop = time.mktime(time.strptime(self.get_argument("end", ""),
                                             "%d/%m/%Y %H:%M:%S"))
        except Exception as e:
            self.application.service.add_notification(int(time.time()),
                "Invalid date(s).", repr(e))
            self.redirect("/contest/%s" % contest_id)
            return

        if start > stop:
            self.application.service.add_notification(int(time.time()),
                "Contest ends before it starts", repr(e))
            self.redirect("/contest/%s" % contest_id)
            return

        self.contest.name = name
        self.contest.description = description
        self.contest.token_initial = token_initial
        self.contest.token_max = token_max
        self.contest.token_total = token_total
        self.contest.token_min_interval = token_min_interval
        self.contest.token_gen_time = token_gen_time
        self.contest.token_gen_number = token_gen_number
        self.contest.start = start
        self.contest.stop = stop

        self.sql_session.commit()
        self.redirect("/contest/%s" % contest_id)


class AddAnnouncementHandler(BaseHandler):
    """Called to actually add an announcement

    """
    def post(self, contest_id):
        self.retrieve_contest(contest_id)
        subject = self.get_argument("subject", "")
        text = self.get_argument("text", "")
        if subject != "":
            ann = Announcement(int(time.time()), subject, text, self.contest)
            self.sql_session.add(ann)
            self.sql_session.commit()
        self.redirect("/announcements/%s" % contest_id)


class RemoveAnnouncementHandler(BaseHandler):
    """Called to remove an announcement.

    """
    def get(self, ann_id):
        ann = Announcement.get_from_id(ann_id, self.sql_session)
        if ann is None:
            raise tornado.web.HTTPError(404)
        contest_id = str(ann.contest.id)
        self.sql_session.delete(ann)
        self.sql_session.commit()
        self.redirect("/announcements/%s" % contest_id)


class UserViewHandler(BaseHandler):
    """Shows the details of a single user (submissions, questions,
    messages, and allows to send the latters).

    """
    def get(self, user_id):

        user = User.get_from_id(user_id, self.sql_session)
        if user is None:
            raise tornado.web.HTTPError(404)
        self.contest = user.contest
        r_params = self.render_params()
        r_params["selected_user"] = user
        r_params["submissions"] = user.submissions
        self.render("user.html", **r_params)

    def post(self, user_id):
        user = User.get_from_id(user_id, self.sql_session)
        if user is None:
            raise tornado.web.HTTPError(404)
        self.contest = user.contest
        user.real_name = self.get_argument("real_name", user.real_name)
        username = self.get_argument("username", user.username)

        # Prevent duplicate usernames in the contest.
        for u in self.contest.users:
            if u.username != user.username and u.username == username:
                self.application.service.add_notification(int(time.time()),
                    "Duplicate username",
                    "The requested username already exists in the contest.")
                self.redirect("/user/%s" % user.id)
                return
        user.username = username

        user.password = self.get_argument("password", user.password)

        # FIXME: Check IP validity
        user.ip = self.get_argument("ip", user.ip)

        user.hidden = self.get_argument("hidden", False) != False

        self.sql_session.commit()
        self.application.service.add_notification(int(time.time()),
            "User updated successfully.", "")
        self.redirect("/user/%s" % user.id)

class AddUserHandler(SimpleContestHandler("add_user.html")):
    def post(self,contest_id):
        self.retrieve_contest(contest_id)

        real_name = self.get_argument("real_name", "")
        username = self.get_argument("username", "")

        # Prevent duplicate usernames in the contest.
        for u in self.contest.users:
            if u.username == username:
                self.application.service.add_notification(int(time.time()),
                    "Duplicate username",
                    "The requested username already exists in the contest.")
                self.redirect("/add_user/%d" % self.contest.id)
                return

        password = self.get_argument("password", "")

        # FIXME: Check IP validity
        ip = self.get_argument("ip", "0.0.0.0")

        hidden = self.get_argument("hidden", False) != False

        user = User(real_name, username, password = password, ip = ip,
            hidden = hidden, contest = self.contest)
        self.sql_session.add(user)
        self.sql_session.commit()
        self.application.service.add_notification(int(time.time()),
            "User added successfully.",
            "")
        self.redirect("/user/%s" % user.id)

class SubmissionViewHandler(BaseHandler):
    """Shows the details of a submission. All data is already present
    in the list of the submissions of the task or of the user, but we
    need a place where to link messages like 'Submission 42 failed to
    compile please check'.

    """
    def get(self, submission_id):
        submission = Submission.get_from_id(submission_id, self.sql_session)
        if submission is None:
            raise tornado.web.HTTPError(404)
        self.contest = submission.user.contest
        r_params = self.render_params()
        r_params["s"] = submission
        self.render("submission.html", **r_params)


class SubmissionFileHandler(FileHandler):
    """Shows a submission file.

    """

    # FIXME: Replace with FileFromDigestHandler?

    @tornado.web.asynchronous
    def get(self, file_id):

        sub_file = File.get_from_id(file_id, self.sql_session)

        if sub_file is None:
            raise tornado.web.HTTPError(404)

        submission = sub_file.submission
        real_filename = sub_file.filename
        if submission.language is not None:
            real_filename = real_filename.replace("%l", submission.language)
        digest = sub_file.digest
        self.sql_session.close()

        self.fetch(sub_file.digest, "text/plain", real_filename)


class QuestionsHandler(BaseHandler):
    """Page to see and send messages to all the contestants.

    """
    def get(self, contest_id):
        self.retrieve_contest(contest_id)
        r_params = self.render_params()
        r_params["questions"] = self.sql_session.query(Question)\
            .join(User).filter(User.contest_id == contest_id).all()
        self.render("questions.html", **r_params)


class QuestionReplyHandler(BaseHandler):
    """Called when the manager replies to a question made by a user.

    """
    def post(self, question_id):
        ref = self.get_argument("ref", "/")

        question = Question.get_from_id(question_id, self.sql_session)
        if question is None:
            raise tornado.web.HTTPError(404)

        reply_subject_code = self.get_argument("reply_question_quick_answer",
                                               "")
        question.reply_text = self.get_argument("reply_question_text", "")

        # Ignore invalid answers
        if reply_subject_code not in WebConfig.quick_answers:
            question.reply_subject = ""
        else:
            # Quick answer given, ignore long answer.
            question.reply_subject = \
                WebConfig.quick_answers[reply_subject_code]
            question.reply_text = ""

        question.reply_timestamp = int(time.time())

        self.sql_session.commit()

        logger.warning("Reply sent to user %s for question '%s'." %
                       (question.user.username, question.subject))

        self.redirect(ref)


class MessageHandler(BaseHandler):
    """Called when a message is sent to a specific user.

    """

    def post(self, user_id):
        r_params = self.render_params()

        user = User.get_from_id(user_id, self.sql_session)
        if user is None:
            raise tornado.web.HTTPError(404)

        message = Message(int(time.time()),
                          self.get_argument("message_subject", ""),
                          self.get_argument("message_text", ""),
                          user=user)
        self.sql_session.add(message)
        self.sql_session.commit()

        logger.warning("Message submitted to user %s."
                       % user)

        self.redirect("/user/%s" % user_id)


class SubmissionReevaluateHandler(BaseHandler):
    """Ask ES to reevaluate the specific submission.

    """

    @tornado.web.asynchronous
    def get(self, submission_id):
        submission = Submission.get_from_id(submission_id, self.sql_session)
        if submission is None:
            raise tornado.web.HTTPError(404)

        self.submission = submission
        self.contest = submission.task.contest

        submission.invalid()
        self.sql_session.commit()
        self.application.service.ES.new_submission(submission_id=submission.id)
        self.redirect("/submission/%s" % submission.id)


class UserReevaluateHandler(BaseHandler):

    @tornado.web.asynchronous
    def get(self, user_id):
        self.user_id = user_id
        user = User.get_from_id(user_id, self.sql_session)
        if user is None:
            raise tornado.web.HTTPError(404)

        self.contest = user.contest

        self.pending_requests = len(user.submissions)
        for s in user.submissions:
            s.invalid()
            self.sql_session.commit()
            self.application.service.ES.new_submission(submission_id=s.id)

        self.redirect("/user/%s" % self.user_id)


class TaskReevaluateHandler(BaseHandler):

    @tornado.web.asynchronous
    def get(self, task_id):
        self.task_id = task_id
        task = Task.get_from_id(task_id, self.sql_session)
        if task is None:
            raise tornado.web.HTTPError(404)

        self.contest = task.contest

        self.pending_requests = len(task.submissions)
        for s in task.submissions:
            s.invalid()
            self.sql_session.commit()
            self.application.service.ES.new_submission(submission_id=s.id)

        self.redirect("/task/%s" % self.task_id)


class FileFromDigestHandler(FileHandler):

    @tornado.web.asynchronous
    def get(self, digest, filename):
        #TODO: Accept a MIME type
        self.fetch(digest, "text/plain", filename)


class NotificationsHandler(BaseHandler):
    """Displays notifications.

    """
    def get(self):
        timestamp = int(time.time())
        res = []
        last_notification = float(self.get_argument("last_notification", "0"))

        # Keep "== None" in filter arguments
        questions = self.sql_session.query(Question)\
                      .filter(Question.reply_timestamp == None)\
                      .filter(Question.question_timestamp > last_notification)\
                      .all()

        for question in questions:
            res.append({"type": "new_question",
                        "timestamp": question.question_timestamp,
                        "subject": question.subject,
                        "text": question.text})

        # Simple notifications
        for notification in self.application.service.notifications:
            res.append({"type": "notification",
                        "timestamp": notification[0],
                        "subject": notification[1],
                        "text": notification[2]})
        self.application.service.notifications = []

        self.write(simplejson.dumps(res))


handlers = [
    (r"/",         MainHandler),
    (r"/([0-9]+)", MainHandler),
    (r"/contest/([0-9]+)",       SimpleContestHandler("contest.html")),
    (r"/announcements/([0-9]+)", SimpleContestHandler("announcements.html")),
    (r"/userlist/([0-9]+)",      SimpleContestHandler("userlist.html")),
    (r"/tasklist/([0-9]+)",      SimpleContestHandler("tasklist.html")),
    (r"/contest/add",           AddContestHandler),
    (r"/contest/edit/([0-9]+)", EditContestHandler),
    (r"/task/([0-9]+)",           TaskViewHandler),
    (r"/task/([0-9]+)/statement", TaskStatementViewHandler),
    (r"/add_task/([0-9]+)",                AddTaskHandler),
    (r"/add_statement/([0-9]+)",       AddStatementHandler),
    (r"/add_attachment/([0-9]+)",       AddAttachmentHandler),
    (r"/delete_attachment/([0-9]+)",       DeleteAttachmentHandler),
    (r"/add_manager/([0-9]+)",       AddManagerHandler),
    (r"/delete_manager/([0-9]+)",       DeleteManagerHandler),
    (r"/add_testcase/([0-9]+)",       AddTestcaseHandler),
    (r"/delete_testcase/([0-9]+)",       DeleteTestcaseHandler),
    (r"/user/([a-zA-Z0-9_-]+)",   UserViewHandler),
    (r"/add_user/([0-9]+)",       AddUserHandler),
    (r"/reevaluate/task/([0-9]+)",               TaskReevaluateHandler),
    (r"/reevaluate/user/([0-9]+)",               UserReevaluateHandler),
    (r"/reevaluate/submission/([a-zA-Z0-9_-]+)", SubmissionReevaluateHandler),
    (r"/add_announcement/([0-9]+)",    AddAnnouncementHandler),
    (r"/remove_announcement/([0-9]+)", RemoveAnnouncementHandler),
    (r"/submission/([0-9]+)",                SubmissionViewHandler),
    (r"/submission_file/([a-zA-Z0-9_.-]+)",  SubmissionFileHandler),
    (r"/file/([a-f0-9]+)/([a-zA-Z0-9_.-]+)", FileFromDigestHandler),
    (r"/message/([a-zA-Z0-9_-]+)",  MessageHandler),
    (r"/question/([a-zA-Z0-9_-]+)", QuestionReplyHandler),
    (r"/questions/([0-9]+)",        QuestionsHandler),
    (r"/resources",                 ResourcesHandler),
    (r"/resources/([0-9]+)",        ResourcesHandler),
    (r"/notifications",             NotificationsHandler),
    ]


def main():
    import sys
    if len(sys.argv) < 2:
        print sys.argv[0], "shard"
    else:
        AdminWebServer(int(sys.argv[1])).run()


if __name__ == "__main__":
    main()
