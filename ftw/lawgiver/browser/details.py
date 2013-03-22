from Products.CMFCore.utils import getToolByName
from Products.GenericSetup.utils import importObjects
from Products.statusmessages.interfaces import IStatusMessage
from ftw.lawgiver import _
from ftw.lawgiver.interfaces import IActionGroupRegistry
from ftw.lawgiver.interfaces import IWorkflowGenerator
from ftw.lawgiver.interfaces import IWorkflowSpecificationDiscovery
from ftw.lawgiver.wdl.interfaces import IWorkflowSpecificationParser
from zope.component import getMultiAdapter
from zope.component import getUtilitiesFor
from zope.component import getUtility
from zope.interface import implementer
from zope.publisher.browser import BrowserView
from zope.publisher.interfaces import IPublishTraverse
from zope.security.interfaces import IPermission
import os.path


@implementer(IPublishTraverse)
class SpecDetails(BrowserView):

    def __init__(self, context, request):
        super(SpecDetails, self).__init__(context, request)
        self._spec_hash = None
        self.specification = None

    def publishTraverse(self, request, name):
        # stop traversing, we have arrived
        request['TraversalRequestNameStack'] = []

        self._spec_hash = name
        return self

    def __call__(self, *args, **kwargs):
        self.specification = self._load_specification()

        if self.specification:
            if 'write_workflow' in self.request.form:
                self.write_workflow()

            if 'write_and_import' in self.request.form:
                self.write_and_import_workflow()

        if 'update_security' in self.request.form:
            self.update_security()

        return super(SpecDetails, self).__call__(*args, **kwargs)

    def write_workflow(self):
        generator = getUtility(IWorkflowGenerator)

        with open(self.get_definition_path(), 'w+') as result_file:
            generator(self.workflow_name(), self.specification, result_file)

        IStatusMessage(self.request).add(
            _(u'info_workflow_generated',
              default=u'The workflow was generated to ${path}.',
              mapping={'path': self.get_definition_path()}))

    def write_and_import_workflow(self):
        self.write_workflow()

        setup_tool = getToolByName(self.context, 'portal_setup')
        profile_id = self._find_profile_name_for_workflow()
        import_context = setup_tool._getImportContext(
            profile_id, None, None)

        workflow = self._get_workflow_obj()
        parent_path = 'workflows/'
        importObjects(workflow, parent_path, import_context)

        IStatusMessage(self.request).add(
            _(u'info_workflow_imported',
              default=u'Workflow ${wfname} successfully imported.',
              mapping={'wfname': self.workflow_name()}))

    def update_security(self):
        wftool = getToolByName(self.context, 'portal_workflow')
        updated_objects = wftool.updateRoleMappings()

        IStatusMessage(self.request).add(
            _(u'info_security_updated',
              default=u'Security update: ${amount} objects updated.',
              mapping={'amount': updated_objects}))

    def _get_workflow_obj(self):
        wftool = getToolByName(self.context, 'portal_workflow')
        return wftool[self.workflow_name()]

    def _find_profile_name_for_workflow(self):
        setup_tool = getToolByName(self.context, 'portal_setup')
        profile_path = os.path.abspath(os.path.join(
                os.path.dirname(self.get_definition_path()),
                '..', '..'))

        for profile in setup_tool.listProfileInfo():
            if profile.get('path') == profile_path:
                return 'profile-%s' % profile.get('id')

        raise AttributeError('Profile for workflow %s not found' % (
                self.get_definition_path()))

    def _load_specification(self):
        parser = getUtility(IWorkflowSpecificationParser)
        path = self.get_spec_path()

        with open(path) as specfile:
            try:
                return parser(specfile)
            except Exception, exc:
                IStatusMessage(self.request).add(
                    _(u'error_parsing_error',
                      default=u'The specification file could not be'
                      u' parsed: ${error}',
                      mapping={'error': str(exc)}),
                    type='error')
                return None

    def is_workflow_installed(self):
        wftool = getToolByName(self.context, 'portal_workflow')
        return wftool.getWorkflowById(self.workflow_name()) and True or False

    def workflow_name(self):
        path = self.get_spec_path()
        return os.path.basename(os.path.dirname(path))

    def raw_specification(self):
        path = self.get_spec_path()
        with open(path) as specfile:
            # handle errors
            return specfile.read()

    def get_spec_path(self):
        discovery = getMultiAdapter((self.context, self.request),
                                    IWorkflowSpecificationDiscovery)

        assert self._spec_hash, \
            'Spec hash was not set in traversal: "%s"' % str(self._spec_hash)

        path = discovery.unhash(self._spec_hash)
        if path is None:
            raise ValueError(
                'Could not find any specification with hash "%s"' % str(
                    self._spec_hash))

        return path

    def get_definition_path(self):
        """Path to workflow definition file.
        """
        return os.path.join(os.path.dirname(self.get_spec_path()),
                            'definition.xml')

    def get_permissions(self):
        managed = {}
        unmanaged = []
        workflow_name = self.workflow_name()

        registry = getUtility(IActionGroupRegistry)
        for _name, permission in getUtilitiesFor(IPermission):
            if ',' in permission.title:
                # permissions with commas in the title are not supported
                # because it conflicts with the comma separated ZCML.
                # e.g. "Public, everyone can access"
                continue

            group = registry.get_action_group_for_permission(
                permission.title, workflow_name=workflow_name)

            if not group:
                unmanaged.append(permission.title)

            else:
                if group not in managed:
                    managed[group] = []
                managed[group].append(permission.title)

        map(list.sort, managed.values())
        unmanaged.sort()

        return {'managed': managed,
                'unmanaged': unmanaged}