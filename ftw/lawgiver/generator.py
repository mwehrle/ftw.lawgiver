from ftw.lawgiver.interfaces import IActionGroupRegistry
from ftw.lawgiver.interfaces import IPermissionCollector
from ftw.lawgiver.interfaces import IWorkflowGenerator
from ftw.lawgiver.variables import VARIABLES
from lxml import etree
from lxml import html
from plone.i18n.normalizer.interfaces import INormalizer
from zope.component import getUtility
from zope.interface import implements


class WorkflowGenerator(object):

    implements(IWorkflowGenerator)

    def __init__(self):
        self.workflow_id = None
        self.specification = None
        self.managed_permissions = None
        self.document = None

    def __call__(self, workflow_id, specification):
        self.workflow_id = workflow_id
        self.specification = specification
        self.managed_permissions = sorted(
            getUtility(IPermissionCollector).collect(workflow_id))

        doc = self._create_document()
        self.document = doc

        status_nodes = {}
        for status in sorted(specification.states.values(),
                             key=lambda status: status.title):
            status_nodes[status] = self._add_status(doc, status)

        transition_nodes = {}
        for transition in sorted(specification.transitions,
                                 key=lambda transition: transition.title):
            transition_nodes[transition] = self._add_transition(
                doc, transition)

        self._apply_specification_statements(status_nodes, transition_nodes)

        self._add_variables(doc)
        return self

    def write(self, result_stream):
        if self.document is None:
            raise RuntimeError(
                'The specification was not yet generated.'
                ' Call the generator first.')

        etree.ElementTree(self.document).write(result_stream,
                                               pretty_print=True,
                                               xml_declaration=True,
                                               encoding='utf-8')

    def get_translations(self, workflow_id, specification):
        self.workflow_id = workflow_id

        result = {}

        for status in specification.states.values():
            result[self._status_id(status)] = status.title

        for transition in specification.transitions:
            result[self._transition_id(transition)] = transition.title

        self.workflow_id = None
        return result

    def get_states(self, workflow_id, specification):
        self.workflow_id = workflow_id
        result = []

        for status in specification.states.values():
            result.append(self._status_id(status))

        return result

    def _create_document(self):
        root = etree.Element("dc-workflow")
        root.set('workflow_id', self.workflow_id)
        root.set('title', self.specification.title.decode('utf-8'))
        root.set('description', self.specification.description and
                 self.specification.description.decode('utf-8') or '')

        root.set('initial_state', self._status_id(
                self.specification.get_initial_status()))

        root.set('state_variable', 'review_state')
        root.set('manager_bypass', 'True')

        for permission in self.managed_permissions:
            etree.SubElement(root, 'permission').text = permission.decode(
                'utf-8')

        return root

    def _add_status(self, doc, status):
        node = etree.SubElement(doc, 'state')
        node.set('state_id', self._status_id(status))
        node.set('title', status.title.decode('utf-8'))

        for transition in self.specification.transitions:
            assert transition.src_status is not None, \
                '%s has improperly defined src_status' % str(transition)
            if transition.src_status != status:
                continue

            exit_trans = etree.SubElement(node, 'exit-transition')
            exit_trans.set('transition_id', self._transition_id(transition))

        return node

    def _add_transition(self, doc, transition):
        node = etree.SubElement(doc, 'transition')

        node.set('new_state', self._status_id(transition.dest_status))
        node.set('title', transition.title.decode('utf-8'))
        node.set('transition_id', self._transition_id(transition))

        node.set('after_script', '')
        node.set('before_script', '')
        node.set('trigger', 'USER')

        action = etree.SubElement(node, 'action')
        action.set('category', 'workflow')
        action.set('icon', '')

        url_struct = self.specification.custom_transition_url or \
            '%%(content_url)s/content_status_modify' + \
            '?workflow_action=%(transition)s'

        action.set('url', url_struct % {
                'transition': self._transition_id(transition)})
        action.text = transition.title.decode('utf-8')

        return node

    def _apply_specification_statements(self, status_nodes,
                                        transition_nodes):
        transition_statements = dict([(status, set()) for status in
                                      status_nodes.keys()])

        per_status_role_inheritance = {}

        for status, snode in status_nodes.items():
            statements = set(status.statements) | set(
                self.specification.generals)

            role_inheritance = self._get_merged_role_inheritance(status)
            per_status_role_inheritance[status] = role_inheritance

            status_stmts, trans_stmts = self._distinguish_statements(
                statements)
            transition_statements[status].update(trans_stmts)

            self._apply_status_statements(snode, status_stmts,
                                          role_inheritance)

            self._add_worklist_when_necessary(status, role_inheritance)

        self._apply_transition_statements(transition_statements,
                                          transition_nodes,
                                          per_status_role_inheritance)

    def _apply_status_statements(self, snode, statements, role_inheritance):
        for permission in self.managed_permissions:
            pnode = etree.SubElement(snode, 'permission-map')
            pnode.set('name', permission)
            pnode.set('acquired', 'False')

            roles = self._get_roles_for_permission(permission, statements)
            roles = resolve_inherited_roles(roles, role_inheritance)

            for role in roles:
                rolenode = etree.SubElement(pnode, 'permission-role')
                rolenode.text = role.decode('utf-8')

    def _apply_transition_statements(self, statements, nodes,
                                     per_status_role_inheritance):
        for transition, node in nodes.items():
            guards = etree.SubElement(node, 'guard')

            role_inheritance = per_status_role_inheritance.get(
                transition.src_status, [])

            roles = []
            for customer_role, action in statements[transition.src_status]:
                if action != transition.title:
                    continue

                role = self.specification.role_mapping[customer_role]
                roles.append(role)

            roles = resolve_inherited_roles(roles, role_inheritance)

            for role in roles:
                rolenode = etree.SubElement(guards, 'guard-role')
                rolenode.text = role.decode('utf-8')

            if len(guards) == 0:
                # Disable the transition by a condition guard, because there
                # were no statements about who can do the transtion.
                xprnode = etree.SubElement(guards, 'guard-expression')
                xprnode.text = u'python: False'

    def _add_worklist_when_necessary(self, status, role_inheritance):
        if not status.worklist_viewers:
            return False

        worklist = etree.SubElement(self.document, 'worklist')
        worklist.set('title', '')
        worklist.set('worklist_id', self._worklist_id(status))

        action = etree.SubElement(worklist, 'action')
        action.set('category', 'global')
        action.set('icon', '')
        action.set('url', '%%(portal_url)s/search?review_state=%s' % (
                self._status_id(status)))
        action.text = '%s (%%(count)d)' % status.title.decode('utf-8')

        match = etree.SubElement(worklist, 'match')
        match.set('name', 'review_state')
        match.set('values', self._status_id(status))

        guards = etree.SubElement(worklist, 'guard')

        roles = [self.specification.role_mapping[crole]
                 for crole in status.worklist_viewers]
        roles = resolve_inherited_roles(roles, role_inheritance)

        for role in roles:
            rolenode = etree.SubElement(guards, 'guard-role')
            rolenode.text = role.decode('utf-8')

    def _get_roles_for_permission(self, permission, statements):
        agregistry = getUtility(IActionGroupRegistry)
        action_group = agregistry.get_action_group_for_permission(
            permission, self.workflow_id)

        customer_roles = (role for (role, group) in statements
                          if group == action_group)

        plone_roles = (self.specification.role_mapping[cr]
                       for cr in customer_roles)
        return sorted(plone_roles)

    def _distinguish_statements(self, statements):
        """Accepts a list of statements (tuples with customer role and action)
        and turns it into two lists, the first with action group statements,
        the second with transition statements.
        """

        action_group_statements = []
        transition_statements = []

        agregistry = getUtility(IActionGroupRegistry)
        action_groups = agregistry.get_action_groups_for_workflow(
            self.workflow_id)

        for customer_role, action in statements:
            if self._find_transition_by_title(action):
                transition_statements.append((customer_role, action))

            elif action in action_groups:
                action_group_statements.append((customer_role, action))

            else:
                raise Exception(
                    'Action "%s" is neither action group nor transition.' % (
                        action))

        return action_group_statements, transition_statements

    def _find_transition_by_title(self, title):
        for transition in self.specification.transitions:
            if transition.title == title:
                return transition
        return None

    def _add_variables(self, doc):
        # The variables are static - we use always the same.
        for node in html.fragments_fromstring(VARIABLES):
            doc.append(node)

    def _transition_id(self, transition):
        return '%s--TRANSITION--%s--%s_%s' % (
            self.workflow_id,
            self._normalize(transition.title),
            self._normalize(transition.src_status.title),
            self._normalize(transition.dest_status.title))

    def _status_id(self, status):
        return '%s--STATUS--%s' % (
            self.workflow_id, self._normalize(status.title))

    def _worklist_id(self, status):
        return '%s--WORKLIST--%s' % (
            self.workflow_id, self._normalize(status.title))

    def _normalize(self, text):
        if isinstance(text, str):
            text = text.decode('utf-8')

        normalizer = getUtility(INormalizer)
        result = normalizer.normalize(text)
        return result.decode('utf-8')

    def _get_merged_role_inheritance(self, status):
        """
        - merges status role inheritance and global (general)
        role inheritance

        - translates customer roles into plone roles
        """

        customer_roles = set(self.specification.role_inheritance)
        customer_roles.update(set(status.role_inheritance))

        result = []
        for inheritor_role, base_role in customer_roles:
            result.append((
                    self.specification.role_mapping[inheritor_role],
                    self.specification.role_mapping[base_role]))

        return result


def resolve_inherited_roles(roles, role_inheritance):
    if not role_inheritance:
        return roles

    roles = roles[:]

    # role_inheritance is: [('inheritor', 'base'), ('inheritor2', 'base')]
    # make: {'base': ['inheritor', 'inheritor2']}
    base_roles = set(zip(*role_inheritance)[1])
    mapping = dict([(key, []) for key in base_roles])
    for inheritor, base in role_inheritance:
        mapping[base].append(inheritor)

    def _recurse(role, mapping, result):
        if role not in mapping:
            return

        for alias in mapping[role]:
            if alias not in result:
                result.append(alias)
                _recurse(alias, mapping, result)

    for role in roles[:]:
        _recurse(role, mapping, roles)

    return sorted(roles)
