import os

import datetime
from bottle import abort, get, post, put, delete, response, local, request

from codalab.common import PermissionError, UsageError, NotFoundError
from codalab.lib import (
    canonicalize,
    spec_util,
    worksheet_util,
)
from codalab.lib import formatting
from codalab.lib.canonicalize import HOME_WORKSHEET
from codalab.lib.server_util import (
    bottle_patch as patch,
    json_api_include,
    query_get_list,
    query_get_bool,
)
from codalab.model.tables import GROUP_OBJECT_PERMISSION_READ
from codalab.objects.permission import (
    check_worksheet_has_all_permission,
    check_worksheet_has_read_permission,
)
from codalab.objects.user import PUBLIC_USER
from codalab.objects.worksheet import Worksheet
from codalab.rest.schemas import WorksheetSchema, WorksheetPermissionSchema, \
    BundleSchema, WorksheetItemSchema
from codalab.rest.users import UserSchema
from codalab.rest.util import (
    get_bundle_infos,
    resolve_owner_in_keywords,
    get_resource_ids)
from codalab.server.authenticated_plugin import AuthenticatedPlugin


@get('/worksheets/<uuid:re:%s>' % spec_util.UUID_STR)
def fetch_worksheet(uuid):
    worksheet = get_worksheet_info(
        uuid,
        fetch_items=True,
        fetch_permission=True,
    )

    # Build response document
    document = WorksheetSchema().dump(worksheet).data

    # Include items
    json_api_include(document, WorksheetItemSchema(), worksheet['items'])

    # Include bundles
    bundle_uuids = {item['bundle_uuid'] for item in worksheet['items']
                    if item['type'] == worksheet_util.TYPE_BUNDLE and item['bundle_uuid'] is not None}
    bundle_infos = get_bundle_infos(bundle_uuids).values()
    json_api_include(document, BundleSchema(), bundle_infos)

    # Include users
    user_ids = {b['owner_id'] for b in bundle_infos}
    user_ids.add(worksheet['owner_id'])
    if user_ids:
        json_api_include(document, UserSchema(), local.model.get_users(user_ids))

    # Include subworksheets
    subworksheet_uuids = {item['subworksheet_uuid']
                          for item in worksheet['items']
                          if item['type'] == worksheet_util.TYPE_WORKSHEET and item['subworksheet_uuid'] is not None}
    json_api_include(document, WorksheetSchema(), local.model.batch_get_worksheets(fetch_items=False, uuid=subworksheet_uuids))

    # FIXME: tokenizing directive args
    # value_obj = formatting.string_to_tokens(value) if type == worksheet_util.TYPE_DIRECTIVE else value

    # Include permissions
    json_api_include(document, WorksheetPermissionSchema(), worksheet['group_permissions'])

    return document


@get('/worksheets')
def fetch_worksheets():
    """
    Fetch bundles by bundle specs OR search keywords.
    """
    keywords = query_get_list('keywords')
    specs = query_get_list('specs')
    base_worksheet_uuid = request.query.get('base')

    if specs:
        uuids = [get_worksheet_uuid_or_create(base_worksheet_uuid, spec) for spec in specs]
        worksheets = [w.to_dict() for w in local.model.batch_get_worksheets(fetch_items=False, uuid=uuids)]
    else:
        keywords = resolve_owner_in_keywords(keywords)
        worksheets = local.model.search_worksheets(request.user.user_id, keywords)

    # Build response document
    document = WorksheetSchema(many=True).dump(worksheets).data

    # Include users
    owner_ids = {w['owner_id'] for w in worksheets}
    if owner_ids:
        json_api_include(document, UserSchema(), local.model.get_users(owner_ids))

    # Include permissions
    for w in worksheets:
        if 'group_permissions' in w:
            json_api_include(document, WorksheetPermissionSchema(), w['group_permissions'])

    return document


@post('/worksheets', apply=AuthenticatedPlugin())
def create_worksheets():
    # TODO: support more attributes
    worksheets = WorksheetSchema(
        strict=True, many=True  # only allow name for now
    ).load(request.json).data

    for w in worksheets:
        w['uuid'] = new_worksheet(w['name'])

    return WorksheetSchema(many=True).dump(worksheets).data


@post('/worksheets/<uuid:re:%s>/raw' % spec_util.UUID_STR)
def update_worksheet_raw(uuid):
    lines = request.body.read().split(os.linesep)
    new_items, commands = worksheet_util.parse_worksheet_form(lines, local.model, request.user, uuid)
    worksheet_info = get_worksheet_info(uuid, fetch_items=True)
    update_worksheet_items(worksheet_info, new_items)
    return {
        'data': {
            'commands': commands
        }
    }


@patch('/worksheets', apply=AuthenticatedPlugin())
def update_worksheets():
    """
    Bulk update worksheets metadata.
    """
    worksheet_updates = WorksheetSchema(
        strict=True, many=True,
    ).load(request.json, partial=True).data

    for w in worksheet_updates:
        update_worksheet_metadata(w['uuid'], w)

    return WorksheetSchema(many=True).dump(worksheet_updates).data


@delete('/worksheets', apply=AuthenticatedPlugin())
def delete_worksheets():
    """
    Delete the bundles specified.
    If |force|, allow deletion of bundles that have descendants or that appear across multiple worksheets.
    If |recursive|, add all bundles downstream too.
    If |data-only|, only remove from the bundle store, not the bundle metadata.
    If |dry-run|, just return list of bundles that would be deleted, but do not actually delete.
    """
    uuids = get_resource_ids(request.json, 'worksheets')
    force = query_get_bool('force', default=False)
    for uuid in uuids:
        delete_worksheet(uuid, force)


@post('/worksheet-items', apply=AuthenticatedPlugin())
def create_worksheet_items():
    """
    Bulk add worksheet items.

    |replace| - Replace existing items in host worksheets. Default is False.
    """
    replace = query_get_bool('replace', False)

    new_items = WorksheetItemSchema(
        strict=True, many=True,
    ).load(request.json).data

    worksheet_to_items = {}
    for item in new_items:
        worksheet_to_items.setdefault(item['worksheet_uuid'], []).append(item)

    for worksheet_uuid, items in worksheet_to_items.iteritems():
        worksheet_info = get_worksheet_info(worksheet_uuid, fetch_items=True)
        if replace:
            # Replace items in the worksheet
            update_worksheet_items(worksheet_info,
                                   [Worksheet.Item.as_tuple(i) for i in items],
                                   convert_items=False)
        else:
            # Append items to the worksheet
            for item in items:
                add_worksheet_item(worksheet_uuid, Worksheet.Item.as_tuple(item))

    return WorksheetItemSchema(many=True).dump(new_items).data


@post('/worksheet-permissions', apply=AuthenticatedPlugin())
def set_worksheet_permissions():
    """
    Bulk set worksheet permissions.
    """
    new_permissions = WorksheetPermissionSchema(
        strict=True, many=True,
    ).load(request.json).data

    for p in new_permissions:
        worksheet = local.model.get_worksheet(p['object_uuid'], fetch_items=False)
        set_worksheet_permission(worksheet, p['group_uuid'], p['permission'])
    return WorksheetPermissionSchema(many=True).dump(new_permissions).data


#############################################################
#  WORKSHEET HELPER FUNCTIONS
#############################################################


def get_worksheet_info(uuid, fetch_items=False, fetch_permission=True, legacy=False):
    """
    The returned info object contains items which are (bundle_info, subworksheet_info, value_obj, type).
    """
    worksheet = local.model.get_worksheet(uuid, fetch_items=fetch_items)
    check_worksheet_has_read_permission(local.model, request.user, worksheet)

    # Create the info by starting out with the metadata.
    result = worksheet.to_dict(legacy=legacy)

    # TODO(sckoo): Legacy requirement, remove when BundleService is deprecated
    if legacy:
        if fetch_items:
            result['items'] = convert_items_from_db(result['items'])
        owner = local.model.get_user(user_id=result['owner_id'])
        result['owner_name'] = owner.user_name

    # Note that these group_permissions is universal and permissions are relative to the current user.
    # Need to make another database query.
    if fetch_permission:
        result['group_permissions'] = local.model.get_group_worksheet_permissions(
            request.user.user_id, worksheet.uuid)
        result['permission'] = local.model.get_user_worksheet_permissions(
            request.user.user_id, [worksheet.uuid], {worksheet.uuid: worksheet.owner_id}
        )[worksheet.uuid]

    return result


# TODO(sckoo): Legacy requirement, remove when BundleService is deprecated
def convert_items_from_db(items):
    """
    Helper function.
    (bundle_uuid, subworksheet_uuid, value, type) -> (bundle_info, subworksheet_info, value_obj, type)
    """
    # Database only contains the uuid; need to expand to info.
    # We need to do to convert the bundle_uuids into bundle_info dicts.
    # However, we still make O(1) database calls because we use the
    # optimized batch_get_bundles multiget method.
    bundle_uuids = set(
        bundle_uuid for (bundle_uuid, subworksheet_uuid, value, type) in items
        if bundle_uuid is not None
    )

    bundle_dict = get_bundle_infos(bundle_uuids)

    # Go through the items and substitute the components
    new_items = []
    for (bundle_uuid, subworksheet_uuid, value, type) in items:
        bundle_info = bundle_dict.get(bundle_uuid, {'uuid': bundle_uuid}) if bundle_uuid else None
        if subworksheet_uuid:
            try:
                subworksheet_info = local.model.get_worksheet(subworksheet_uuid, fetch_items=False).to_dict(legacy=True)
            except UsageError, e:
                # If can't get the subworksheet, it's probably invalid, so just replace it with an error
                # type = worksheet_util.TYPE_MARKUP
                subworksheet_info = {'uuid': subworksheet_uuid}
                # value = 'ERROR: non-existent worksheet %s' % subworksheet_uuid
        else:
            subworksheet_info = None
        value_obj = formatting.string_to_tokens(value) if type == worksheet_util.TYPE_DIRECTIVE else value
        new_items.append((bundle_info, subworksheet_info, value_obj, type))
    return new_items


def update_worksheet_items(worksheet_info, new_items, convert_items=True):
    """
    Set the worksheet to have items |new_items|.
    """
    worksheet_uuid = worksheet_info['uuid']
    last_item_id = worksheet_info['last_item_id']
    length = len(worksheet_info['items'])
    worksheet = local.model.get_worksheet(worksheet_uuid, fetch_items=False)
    check_worksheet_has_all_permission(local.model, request.user, worksheet)
    worksheet_util.check_worksheet_not_frozen(worksheet)
    try:
        if convert_items:
            new_items = [worksheet_util.convert_item_to_db(item) for item in new_items]
        local.model.update_worksheet_items(worksheet_uuid, last_item_id, length, new_items)
    except UsageError:
        # Turn the model error into a more readable one using the object.
        raise UsageError('%s was updated concurrently!' % (worksheet,))


def update_worksheet_metadata(uuid, info):
    """
    Change the metadata of the worksheet |uuid| to |info|,
    where |info| specifies name, title, owner, etc.
    """
    worksheet = local.model.get_worksheet(uuid, fetch_items=False)
    check_worksheet_has_all_permission(local.model, request.user, worksheet)
    metadata = {}
    for key, value in info.items():
        if key == 'owner_id':
            metadata['owner_id'] = value
        elif key == 'owner_spec':
            # TODO(sckoo): Legacy requirement, remove with BundleService
            metadata['owner_id'] = local.model.find_user(value).user_id
        elif key == 'name':
            ensure_unused_worksheet_name(value)
            metadata[key] = value
        elif key == 'title':
            metadata[key] = value
        elif key == 'tags':
            metadata[key] = value
        elif key == 'freeze':
            # TODO(sckoo): Support for the 'freeze' key is a legacy requirement, remove with BundleService
            metadata['frozen'] = datetime.datetime.now()
        elif key == 'frozen' and value and not worksheet.frozen:
            # ignore the value the client provided, just freeze as long as it's truthy
            metadata['frozen'] = datetime.datetime.now()
    local.model.update_worksheet_metadata(worksheet, metadata)


def set_worksheet_permission(worksheet, group_uuid, permission):
    """
    Give the given |group_uuid| the desired |permission| on |worksheet_uuid|.
    """
    check_worksheet_has_all_permission(local.model, request.user, worksheet)
    local.model.set_group_worksheet_permission(group_uuid, worksheet.uuid, permission)


def populate_worksheet(worksheet, name, title):
    file_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '../objects/' + name + '.ws')
    lines = [line.rstrip() for line in open(file_path, 'r').readlines()]
    items, commands = worksheet_util.parse_worksheet_form(lines, local.model, request.user, worksheet.uuid)
    info = get_worksheet_info(worksheet.uuid, fetch_items=True)
    update_worksheet_items(info, items)
    update_worksheet_metadata(worksheet.uuid, {'title': title})


def ensure_unused_worksheet_name(name):
    """
    Ensure worksheet names are unique.
    Note: for simplicity, we are ensuring uniqueness across the system, even on
    worksheet names that the user may not have access to.
    """
    # If trying to set the name to a home worksheet, then it better be
    # user's home worksheet.
    if spec_util.is_home_worksheet(name) and spec_util.home_worksheet(request.user.user_name) != name:
        raise UsageError('Cannot create %s because this is potentially the home worksheet of another user' % name)
    try:
        canonicalize.get_worksheet_uuid(local.model, request.user, None, name)
        raise UsageError('Worksheet with name %s already exists' % name)
    except NotFoundError:
        pass  # all good!


def new_worksheet(name):
    """
    Create a new worksheet with the given |name|.
    """
    if not request.user.is_authenticated:
        raise PermissionError("You must be logged in to create a worksheet.")
    ensure_unused_worksheet_name(name)

    # Don't need any permissions to do this.
    worksheet = Worksheet({
        'name': name,
        'title': None,
        'frozen': None,
        'items': [],
        'owner_id': request.user.user_id
    })
    local.model.new_worksheet(worksheet)

    # Make worksheet publicly readable by default
    set_worksheet_permission(worksheet, local.model.public_group_uuid,
                             GROUP_OBJECT_PERMISSION_READ)
    if spec_util.is_dashboard(name):
        populate_worksheet(worksheet, 'dashboard', 'CodaLab Dashboard')
    if spec_util.is_public_home(name):
        populate_worksheet(worksheet, 'home', 'Public Home')
    return worksheet.uuid


def get_worksheet_uuid_or_create(base_worksheet_uuid, worksheet_spec):
    """
    Return the uuid of the specified worksheet if it exists.
    If not, create a new worksheet if the specified worksheet is home_worksheet
    or dashboard. Otherwise, throw an error.
    """
    try:
        return canonicalize.get_worksheet_uuid(local.model, request.user, base_worksheet_uuid, worksheet_spec)
    except NotFoundError:
        # A bit hacky, duplicates a bit of canonicalize
        if (worksheet_spec == '' or worksheet_spec == HOME_WORKSHEET) and request.user:
            return new_worksheet(spec_util.home_worksheet(request.user.user_name))
        elif spec_util.is_dashboard(worksheet_spec):
            return new_worksheet(worksheet_spec)
        else:
            raise


def add_worksheet_item(worksheet_uuid, item):
    """
    Add the given item to the worksheet.
    """
    worksheet = local.model.get_worksheet(worksheet_uuid, fetch_items=False)
    check_worksheet_has_all_permission(local.model, request.user, worksheet)
    worksheet_util.check_worksheet_not_frozen(worksheet)
    local.model.add_worksheet_item(worksheet_uuid, item)


def delete_worksheet(uuid, force):
    worksheet = local.model.get_worksheet(uuid, fetch_items=True)
    check_worksheet_has_all_permission(local.model, request.user, worksheet)
    if not force:
        if worksheet.frozen:
            raise UsageError("Can't delete worksheet %s because it is frozen (--force to override)." %
                             worksheet.uuid)
        if len(worksheet.items) > 0:
            raise UsageError("Can't delete worksheet %s because it is not empty (--force to override)." %
                             worksheet.uuid)
    local.model.delete_worksheet(uuid)
