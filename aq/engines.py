import itertools
import json
import os.path
import pprint

import boto3
from six import string_types

from aq import logger, util
from aq.errors import QueryError
from aq.sqlite_util import sqlite3, create_table, insert_all

LOGGER = logger.get_logger()


class BotoSqliteEngine(object):
    def __init__(self, options=None):
        self.options = options if options else {}
        self.debug = options.get('--debug', False)

        self.boto3_session = boto3.Session()
        # dash (-) is not allowed in database name so we use underscore (_) instead in region name
        # throughout this module region name will *always* use underscore
        self.default_region = self.boto3_session.region_name.replace('-', '_')
        self.db = self.init_db()
        # attach the default region too
        self.attach_region(self.default_region)

    def init_db(self):
        util.ensure_data_dir_exists()
        db_path = '~/.aq/{}.db'.format(self.default_region)
        absolute_path = os.path.expanduser(db_path)
        db = sqlite3.connect(absolute_path)
        db.create_function('json_get', 2, json_get)
        return db

    def execute(self, query, metadata):
        LOGGER.info('Executing query: %s', query)
        self.load_tables(query, metadata)
        try:
            cursor = self.db.execute(query)
        except sqlite3.OperationalError as e:
            raise QueryError(str(e))
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        return columns, rows

    def load_tables(self, query, meta):
        """
        Load necessary resources tables into db to execute given query.
        """
        for table in meta.tables:
            self.load_table(table)

    def load_table(self, table):
        """
        Load resources as specified by given table into our db.
        """
        region = table.database if table.database else self.default_region
        resource_name, collection_name = table.table.split('_', 1)
        # we use underscore "_" instead of dash "-" for region name but boto3 need dash
        boto_region_name = region.replace('_', '-') if region else None
        resource = boto3.resource(resource_name, region_name=boto_region_name)
        if not hasattr(resource, collection_name):
            raise QueryError(
                'Unknown collection <{}> of resource <{}>'.format(collection_name, resource_name))

        self.attach_region(region)
        self.refresh_table(region, table.table, resource, getattr(resource, collection_name))

    def attach_region(self, region):
        if not self.is_attached_region(region):
            LOGGER.info('Attaching new database for region: %s', region)
            region_db_file_path = '~/.aq/{}.db'.format(region)
            absolute_path = os.path.expanduser(region_db_file_path)
            self.db.execute('ATTACH DATABASE ? AS ?', (absolute_path, region))

    def is_attached_region(self, region):
        databases = self.db.execute('PRAGMA database_list')
        db_names = (db[1] for db in databases)
        return region in db_names

    def refresh_table(self, schema_name, table_name, resource, collection):
        if not self.is_fresh_enough(schema_name, table_name):
            LOGGER.info('Refreshing table: %s.%s', schema_name, table_name)
            columns = get_columns_list(resource, collection)
            LOGGER.info('Columns list: %s', columns)
            with self.db:
                create_table(self.db, schema_name, table_name, columns)
                items = collection.all()
                # special treatment for tags field
                items = [convert_tags_to_dict(item) for item in items]
                insert_all(self.db, schema_name, table_name, columns, items)

    def is_fresh_enough(self, schema_name, table_name):
        # TODO
        return False


class ObjectProxy(object):
    def __init__(self, source, **replaced_fields):
        self.source = source
        self.replaced_fields = replaced_fields

    def __getattr__(self, item):
        if item in self.replaced_fields:
            return self.replaced_fields[item]
        return getattr(self.source, item)


def convert_tags_to_dict(item):
    """
    Convert AWS inconvenient tags model of a list of {"Key": <key>, "Value": <value>} pairs
    to a dict of {<key>: <value>} for easier querying.

    This returns a proxied object over given item to return a different tags format as the tags
    attribute is read-only and we cannot modify it directly.
    """
    if hasattr(item, 'tags'):
        tags = item.tags
        if isinstance(tags, list):
            tags_dict = {}
            for kv_dict in tags:
                if isinstance(kv_dict, dict) and 'Key' in kv_dict and 'Value' in kv_dict:
                    tags_dict[kv_dict['Key']] = kv_dict['Value']
            return ObjectProxy(item, tags=tags_dict)
    return item


def get_resource_model_attributes(resource, collection):
    service_model = resource.meta.client.meta.service_model
    resource_model = get_resource_model(collection)
    shape_name = resource_model.shape
    shape = service_model.shape_for(shape_name)
    return resource_model.get_attributes(shape)


def get_columns_list(resource, collection):
    resource_model = get_resource_model(collection)
    LOGGER.debug('Resource model: %s', resource_model)

    identifiers = sorted(i.name for i in resource_model.identifiers)
    LOGGER.debug('Model identifiers: %s', identifiers)

    attributes = get_resource_model_attributes(resource, collection)
    LOGGER.debug('Model attributes: %s', pprint.pformat(attributes))

    return list(itertools.chain(identifiers, attributes))


def get_resource_model(collection):
    return collection._model.resource.model


def json_get(serialized_object, field):
    """
    This emulates the HSTORE `->` get value operation.
    It get value from JSON serialized column by given key and return `null` if not present.
    Key can be either an integer for array index access or a string for object field access.

    :return: JSON serialized value of key in object
    """
    # return null if serialized_object is null or "serialized null"
    if serialized_object is None:
        return "null"
    obj = json.loads(serialized_object)
    if obj is None:
        return "null"

    if isinstance(field, int):
        # array index access
        res = obj[field] if 0 <= field < len(obj) else None
    else:
        # object field access
        res = obj.get(field)

    if not isinstance(res, (int, float, string_types)):
        res = json.dumps(res)

    LOGGER.debug('json_get result: %s', res)
    return res
