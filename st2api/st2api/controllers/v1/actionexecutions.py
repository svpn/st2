# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import re
import httplib
import sys
import traceback

import jsonschema
from oslo_config import cfg
from six.moves import http_client

from st2api.controllers.base import BaseRestControllerMixin
from st2api.controllers.resource import ResourceController
from st2api.controllers.v1.executionviews import ExecutionViewsController
from st2api.controllers.v1.executionviews import SUPPORTED_FILTERS
from st2common import log as logging
from st2common.constants.action import LIVEACTION_STATUS_CANCELED, LIVEACTION_STATUS_FAILED
from st2common.constants.action import LIVEACTION_CANCELABLE_STATES
from st2common.exceptions.param import ParamException
from st2common.exceptions.apivalidation import ValueValidationException
from st2common.exceptions.trace import TraceNotFoundException
from st2common.models.api.action import LiveActionAPI
from st2common.models.api.action import LiveActionCreateAPI
from st2common.models.api.base import cast_argument_value
from st2common.models.api.execution import ActionExecutionAPI
from st2common.models.db.auth import UserDB
from st2common.persistence.liveaction import LiveAction
from st2common.persistence.execution import ActionExecution
from st2common.router import abort
from st2common.router import Response
from st2common.services import action as action_service
from st2common.services import executions as execution_service
from st2common.services import trace as trace_service
from st2common.util import isotime
from st2common.util import action_db as action_utils
from st2common.util import param as param_utils
from st2common.util.jsonify import try_loads
from st2common.rbac.types import PermissionType
from st2common.rbac import utils as rbac_utils
from st2common.rbac.utils import assert_user_has_resource_db_permission
from st2common.rbac.utils import assert_user_is_admin_if_user_query_param_is_provided

__all__ = [
    'ActionExecutionsController'
]

LOG = logging.getLogger(__name__)

# Note: We initialize filters here and not in the constructor
SUPPORTED_EXECUTIONS_FILTERS = copy.deepcopy(SUPPORTED_FILTERS)
SUPPORTED_EXECUTIONS_FILTERS.update({
    'timestamp_gt': 'start_timestamp.gt',
    'timestamp_lt': 'start_timestamp.lt'
})

MONITOR_THREAD_EMPTY_Q_SLEEP_TIME = 5
MONITOR_THREAD_NO_WORKERS_SLEEP_TIME = 1


class ActionExecutionsControllerMixin(BaseRestControllerMixin):
    """
    Mixin class with shared methods.
    """

    model = ActionExecutionAPI
    access = ActionExecution

    # A list of attributes which can be specified using ?exclude_attributes filter
    valid_exclude_attributes = [
        'result',
        'trigger_instance'
    ]

    def _handle_schedule_execution(self, liveaction_api, requester_user, context_string=None,
                                   show_secrets=False):
        """
        :param liveaction: LiveActionAPI object.
        :type liveaction: :class:`LiveActionAPI`
        """

        if not requester_user:
            requester_user = UserDB(cfg.CONF.system_user.user)

        # Assert the permissions
        action_ref = liveaction_api.action
        action_db = action_utils.get_action_by_ref(action_ref)
        user = liveaction_api.user or requester_user.name

        assert_user_has_resource_db_permission(user_db=requester_user, resource_db=action_db,
                                               permission_type=PermissionType.ACTION_EXECUTE)

        # Validate that the authenticated user is admin if user query param is provided
        assert_user_is_admin_if_user_query_param_is_provided(user_db=requester_user,
                                                             user=user)

        try:
            return self._schedule_execution(liveaction=liveaction_api,
                                            requester_user=requester_user,
                                            user=user,
                                            context_string=context_string,
                                            show_secrets=show_secrets)
        except ValueError as e:
            LOG.exception('Unable to execute action.')
            abort(http_client.BAD_REQUEST, str(e))
        except jsonschema.ValidationError as e:
            LOG.exception('Unable to execute action. Parameter validation failed.')
            abort(http_client.BAD_REQUEST, re.sub("u'([^']*)'", r"'\1'", e.message))
        except TraceNotFoundException as e:
            abort(http_client.BAD_REQUEST, str(e))
        except ValueValidationException as e:
            raise e
        except Exception as e:
            LOG.exception('Unable to execute action. Unexpected error encountered.')
            abort(http_client.INTERNAL_SERVER_ERROR, str(e))

    def _schedule_execution(self, liveaction, requester_user, user=None, context_string=None,
                            show_secrets=False):
        # Initialize execution context if it does not exist.
        if not hasattr(liveaction, 'context'):
            liveaction.context = dict()

        liveaction.context['user'] = user
        LOG.debug('User is: %s' % liveaction.context['user'])

        # Retrieve other st2 context from request header.
        if context_string:
            context = try_loads(context_string)
            if not isinstance(context, dict):
                raise ValueError('Unable to convert st2-context from the headers into JSON.')
            liveaction.context.update(context)

        # Schedule the action execution.
        liveaction_db = LiveActionAPI.to_model(liveaction)
        liveaction_db, actionexecution_db = action_service.create_request(liveaction_db)

        action_db = action_utils.get_action_by_ref(liveaction_db.action)
        runnertype_db = action_utils.get_runnertype_by_name(action_db.runner_type['name'])

        try:
            liveaction_db.parameters = param_utils.render_live_params(
                runnertype_db.runner_parameters, action_db.parameters, liveaction_db.parameters,
                liveaction_db.context)
        except ParamException:
            # By this point the execution is already in the DB therefore need to mark it failed.
            _, e, tb = sys.exc_info()
            action_service.update_status(
                liveaction=liveaction_db,
                new_status=LIVEACTION_STATUS_FAILED,
                result={'error': str(e), 'traceback': ''.join(traceback.format_tb(tb, 20))})
            # Might be a good idea to return the actual ActionExecution rather than bubble up
            # the execption.
            raise ValueValidationException(str(e))

        liveaction_db = LiveAction.add_or_update(liveaction_db, publish=False)

        _, actionexecution_db = action_service.publish_request(liveaction_db, actionexecution_db)
        mask_secrets = self._get_mask_secrets(requester_user, show_secrets=show_secrets)
        execution_api = ActionExecutionAPI.from_model(actionexecution_db, mask_secrets=mask_secrets)

        return Response(json=execution_api, status=http_client.CREATED)

    def _get_result_object(self, id):
        """
        Retrieve result object for the provided action execution.

        :param id: Action execution ID.
        :type id: ``str``

        :rtype: ``dict``
        """
        fields = ['result']
        action_exec_db = self.access.impl.model.objects.filter(id=id).only(*fields).get()
        return action_exec_db.result

    def _get_children(self, id_, requester_user, depth=-1, result_fmt=None,
                      show_secrets=False):
        # make sure depth is int. Url encoding will make it a string and needs to
        # be converted back in that case.
        depth = int(depth)
        LOG.debug('retrieving children for id: %s with depth: %s', id_, depth)
        descendants = execution_service.get_descendants(actionexecution_id=id_,
                                                        descendant_depth=depth,
                                                        result_fmt=result_fmt)

        mask_secrets = self._get_mask_secrets(requester_user, show_secrets=show_secrets)
        return [self.model.from_model(descendant, mask_secrets=mask_secrets) for
                descendant in descendants]


class BaseActionExecutionNestedController(ActionExecutionsControllerMixin, ResourceController):
    # Note: We need to override "get_one" and "get_all" to return 404 since nested controller
    # don't implement thos methods

    # ResourceController attributes
    query_options = {}
    supported_filters = {}

    def get_all(self):
        abort(httplib.NOT_FOUND)

    def get_one(self, id):
        abort(httplib.NOT_FOUND)


class ActionExecutionChildrenController(BaseActionExecutionNestedController):
    def get_one(self, id, requester_user, depth=-1, result_fmt=None, show_secrets=False):
        """
        Retrieve children for the provided action execution.

        :rtype: ``list``
        """

        instance = self._get_by_id(resource_id=id)

        permission_type = PermissionType.EXECUTION_VIEW
        rbac_utils.assert_user_has_resource_db_permission(user_db=requester_user,
                                                          resource_db=instance,
                                                          permission_type=permission_type)

        return self._get_children(id_=id, depth=depth, result_fmt=result_fmt,
                                  requester_user=requester_user, show_secrets=show_secrets)


class ActionExecutionAttributeController(BaseActionExecutionNestedController):
    valid_exclude_attributes = ['action__pack', 'action__uid'] + \
        ActionExecutionsControllerMixin.valid_exclude_attributes

    def get(self, id, attribute, requester_user):
        """
        Retrieve a particular attribute for the provided action execution.

        Handles requests:

            GET /executions/<id>/attribute/<attribute name>

        :rtype: ``dict``
        """
        fields = [attribute, 'action__pack', 'action__uid']
        fields = self._validate_exclude_fields(fields)
        action_exec_db = self.access.impl.model.objects.filter(id=id).only(*fields).get()

        permission_type = PermissionType.EXECUTION_VIEW
        rbac_utils.assert_user_has_resource_db_permission(user_db=requester_user,
                                                          resource_db=action_exec_db,
                                                          permission_type=permission_type)

        result = getattr(action_exec_db, attribute, None)
        return result


class ActionExecutionReRunController(ActionExecutionsControllerMixin, ResourceController):
    supported_filters = {}
    exclude_fields = [
        'result',
        'trigger_instance'
    ]

    class ExecutionSpecificationAPI(object):
        def __init__(self, parameters=None, tasks=None, reset=None, user=None):
            self.parameters = parameters or {}
            self.tasks = tasks or []
            self.reset = reset or []
            self.user = user

        def validate(self):
            if (self.tasks or self.reset) and self.parameters:
                raise ValueError('Parameters override is not supported when '
                                 're-running task(s) for a workflow.')

            if self.parameters:
                assert isinstance(self.parameters, dict)

            if self.tasks:
                assert isinstance(self.tasks, list)

            if self.reset:
                assert isinstance(self.reset, list)

            if list(set(self.reset) - set(self.tasks)):
                raise ValueError('List of tasks to reset does not match the tasks to rerun.')

            return self

    def post(self, spec_api, id, requester_user, no_merge=False, show_secrets=False):
        """
        Re-run the provided action execution optionally specifying override parameters.

        Handles requests:

            POST /executions/<id>/re_run
        """

        if (spec_api.tasks or spec_api.reset) and spec_api.parameters:
            raise ValueError('Parameters override is not supported when '
                             're-running task(s) for a workflow.')

        if spec_api.parameters:
            assert isinstance(spec_api.parameters, dict)

        if spec_api.tasks:
            assert isinstance(spec_api.tasks, list)

        if spec_api.reset:
            assert isinstance(spec_api.reset, list)

        if list(set(spec_api.reset) - set(spec_api.tasks)):
            raise ValueError('List of tasks to reset does not match the tasks to rerun.')

        no_merge = cast_argument_value(value_type=bool, value=no_merge)
        existing_execution = self._get_one_by_id(id=id, exclude_fields=self.exclude_fields,
                                                 requester_user=requester_user,
                                                 permission_type=PermissionType.EXECUTION_VIEW)

        if spec_api.tasks and existing_execution.runner['name'] != 'mistral-v2':
            raise ValueError('Task option is only supported for Mistral workflows.')

        # Merge in any parameters provided by the user
        new_parameters = {}
        if not no_merge:
            new_parameters.update(getattr(existing_execution, 'parameters', {}))
        new_parameters.update(spec_api.parameters)

        # Create object for the new execution
        action_ref = existing_execution.action['ref']

        # Include additional option(s) for the execution
        context = {
            're-run': {
                'ref': id,
            }
        }

        if spec_api.tasks:
            context['re-run']['tasks'] = spec_api.tasks

        if spec_api.reset:
            context['re-run']['reset'] = spec_api.reset

        # Add trace to the new execution
        trace = trace_service.get_trace_db_by_action_execution(
            action_execution_id=existing_execution.id)

        if trace:
            context['trace_context'] = {'id_': str(trace.id)}

        new_liveaction_api = LiveActionCreateAPI(action=action_ref,
                                                 context=context,
                                                 parameters=new_parameters,
                                                 user=spec_api.user)

        return self._handle_schedule_execution(liveaction_api=new_liveaction_api,
                                               requester_user=requester_user,
                                               show_secrets=show_secrets)


class ActionExecutionsController(ActionExecutionsControllerMixin, ResourceController):
    """
        Implements the RESTful web endpoint that handles
        the lifecycle of ActionExecutions in the system.
    """

    # Nested controllers
    views = ExecutionViewsController()

    children = ActionExecutionChildrenController()
    attribute = ActionExecutionAttributeController()
    re_run = ActionExecutionReRunController()

    # ResourceController attributes
    query_options = {
        'sort': ['-start_timestamp', 'action.ref']
    }
    supported_filters = SUPPORTED_EXECUTIONS_FILTERS
    filter_transform_functions = {
        'timestamp_gt': lambda value: isotime.parse(value=value),
        'timestamp_lt': lambda value: isotime.parse(value=value)
    }

    def get_all(self, requester_user, exclude_attributes=None, sort=None, offset=0, limit=None,
                show_secrets=False, **raw_filters):
        """
        List all executions.

        Handles requests:
            GET /executions[?exclude_attributes=result,trigger_instance]

        :param exclude_attributes: Comma delimited string of attributes to exclude from the object.
        :type exclude_attributes: ``str``
        """
        if exclude_attributes:
            exclude_fields = exclude_attributes.split(',')
        else:
            exclude_fields = None

        exclude_fields = self._validate_exclude_fields(exclude_fields=exclude_fields)

        # Use a custom sort order when filtering on a timestamp so we return a correct result as
        # expected by the user
        query_options = None
        if raw_filters.get('timestamp_lt', None) or raw_filters.get('sort_desc', None):
            query_options = {'sort': ['-start_timestamp', 'action.ref']}
        elif raw_filters.get('timestamp_gt', None) or raw_filters.get('sort_asc', None):
            query_options = {'sort': ['+start_timestamp', 'action.ref']}

        from_model_kwargs = {
            'mask_secrets': self._get_mask_secrets(requester_user, show_secrets=show_secrets)
        }
        return self._get_action_executions(exclude_fields=exclude_fields,
                                           from_model_kwargs=from_model_kwargs,
                                           sort=sort,
                                           offset=offset,
                                           limit=limit,
                                           query_options=query_options,
                                           raw_filters=raw_filters)

    def get_one(self, id, requester_user, exclude_attributes=None, show_secrets=False):
        """
        Retrieve a single execution.

        Handles requests:
            GET /executions/<id>[?exclude_attributes=result,trigger_instance]

        :param exclude_attributes: Comma delimited string of attributes to exclude from the object.
        :type exclude_attributes: ``str``
        """
        if exclude_attributes:
            exclude_fields = exclude_attributes.split(',')
        else:
            exclude_fields = None

        exclude_fields = self._validate_exclude_fields(exclude_fields=exclude_fields)

        from_model_kwargs = {
            'mask_secrets': self._get_mask_secrets(requester_user, show_secrets=show_secrets)
        }
        return self._get_one_by_id(id=id, exclude_fields=exclude_fields,
                                   requester_user=requester_user,
                                   from_model_kwargs=from_model_kwargs,
                                   permission_type=PermissionType.EXECUTION_VIEW)

    def post(self, liveaction_api, requester_user, context_string=None, show_secrets=False):
        return self._handle_schedule_execution(liveaction_api=liveaction_api,
                                               requester_user=requester_user,
                                               context_string=context_string,
                                               show_secrets=show_secrets)

    def delete(self, id, requester_user, show_secrets=False):
        """
        Stops a single execution.

        Handles requests:
            DELETE /executions/<id>

        """
        if not requester_user:
            requester_user = UserDB(cfg.CONF.system_user.user)

        from_model_kwargs = {
            'mask_secrets': self._get_mask_secrets(requester_user, show_secrets=show_secrets)
        }
        execution_api = self._get_one_by_id(id=id, requester_user=requester_user,
                                            from_model_kwargs=from_model_kwargs,
                                            permission_type=PermissionType.EXECUTION_STOP)

        if not execution_api:
            abort(http_client.NOT_FOUND, 'Execution with id %s not found.' % id)

        liveaction_id = execution_api.liveaction['id']
        if not liveaction_id:
            abort(http_client.INTERNAL_SERVER_ERROR,
                  'Execution object missing link to liveaction %s.' % liveaction_id)

        try:
            liveaction_db = LiveAction.get_by_id(liveaction_id)
        except:
            abort(http_client.INTERNAL_SERVER_ERROR,
                  'Execution object missing link to liveaction %s.' % liveaction_id)

        if liveaction_db.status == LIVEACTION_STATUS_CANCELED:
            LOG.info(
                'Action %s already in "canceled" state; \
                returning execution object.' % liveaction_db.id
            )
            return execution_api

        if liveaction_db.status not in LIVEACTION_CANCELABLE_STATES:
            abort(http_client.OK, 'Action cannot be canceled. State = %s.' % liveaction_db.status)

        try:
            (liveaction_db, execution_db) = action_service.request_cancellation(
                liveaction_db, requester_user.name or cfg.CONF.system_user.user)
        except:
            LOG.exception('Failed requesting cancellation for liveaction %s.', liveaction_db.id)
            abort(http_client.INTERNAL_SERVER_ERROR, 'Failed canceling execution.')

        return ActionExecutionAPI.from_model(execution_db,
                                             mask_secrets=from_model_kwargs['mask_secrets'])

    def _get_action_executions(self, exclude_fields=None, sort=None, offset=0, limit=None,
                               query_options=None, raw_filters=None, from_model_kwargs=None):
        """
        :param exclude_fields: A list of object fields to exclude.
        :type exclude_fields: ``list``
        """

        if limit is None:
            limit = self.default_limit

        limit = int(limit)

        LOG.debug('Retrieving all action executions with filters=%s', raw_filters)
        return super(ActionExecutionsController, self)._get_all(exclude_fields=exclude_fields,
                                                                from_model_kwargs=from_model_kwargs,
                                                                sort=sort,
                                                                offset=offset,
                                                                limit=limit,
                                                                query_options=query_options,
                                                                raw_filters=raw_filters)


action_executions_controller = ActionExecutionsController()
action_execution_rerun_controller = ActionExecutionReRunController()
action_execution_attribute_controller = ActionExecutionAttributeController()
action_execution_children_controller = ActionExecutionChildrenController()
