# -*- coding: utf-8 -*-
import logging
from datetime import datetime

from eevee.indexing.utils import delete_index
from eevee.mongo import get_mongo
from eevee.utils import to_timestamp
from elasticsearch import NotFoundError
from elasticsearch_dsl import A, Search

from ckan import logic, plugins
from ckan.lib.search import SearchIndexError
from ckanext.versioned_datastore.interfaces import IVersionedDatastore
from ckanext.versioned_datastore.lib import utils, stats
from ckanext.versioned_datastore.lib.importing import import_resource_data, check_version_is_valid
from ckanext.versioned_datastore.lib.indexing import DatastoreIndex, index_resource
from ckanext.versioned_datastore.lib.search import create_search, prefix_field
from ckanext.versioned_datastore.logic import schema

try:
    enqueue_job = plugins.toolkit.enqueue_job
except AttributeError:
    from ckanext.rq.jobs import enqueue as enqueue_job

log = logging.getLogger(__name__)


@logic.side_effect_free
def datastore_search(context, data_dict):
    '''
    This action allows you to search data in a resource. It is designed to function in a similar way
    to CKAN's core datastore_search but with a few extra bells and whistles, most prominently
    versioning. This allows the resource to be searched at any moment in it's lifespan and have the
    data as it looked at that moment returned, even if it has changed since.

    If the resource to be searched is private then appropriate authorization is required.

    Note that in the parameters listed below spaces should only be included if part of a field name,
    so, for example, don't include any spaces in comma separated lists unless needed.

    :param resource_id: id of the resource to be searched against
    :type resource_id: string
    :param q: full text query. If a string is passed, all fields are searched with the value. If a
          dict is passed each of the fields and values contained within will be searched as
          required (e.g. {"field1": "a", "field2": "b"}).
    :type q: string or dictionary
    :param filters: a dictionary of conditions that must be met to match a record
                    (e.g {"field1": "a", "field2": "b"}) (optional)
    :type filters: dictionary
    :param after: search_after value for elasticsearch to paginate from (optional). Use this
                  mechanism to do deep (beyond 10000 values) pagination. The values have to match
                  the sort currently in use and therefore it's recommended that this value is not
                  built but rather passed from the previous result's 'after' key.
    :type after: a list of values
    :param limit: maximum number of records to return (optional, default: 100)
    :type limit: int
    :param offset: offset this number of records (optional)
    :type offset: int
    :param fields: fields to return for each record (optional, default: all fields are returned)
    :type fields: list or comma separated string
    :param sort: list of field names with ordering. Ordering is ascending by default, if descending
                 is required, add "desc" after the field name
                 e.g.: "fieldname1,fieldname2 desc" sorts by fieldname1 asc and fieldname2 desc
    :type sort: list or comma separated string
    :param version: version to search at, if not provided the current version of the data is
                   searched.
    :type version: int, number of milliseconds (not seconds!) since UNIX epoch
    :param facets: if present, the top 10 most frequent values for each of the fields in this list
                   will be returned along with estimated counts for each value. Calculating these
                   results has a reasonable overhead so only include this parameter if you need it
    :type facets: list or comma separated string
    :param facet_limits: if present, specifies the number of top values to retrieve for the facets
                        listed within. The default number will be used if this parameter is not
                        specified or if a facet in the facets list does not appear in this dict. For
                        example, with this facet list ['facet1', 'facet2', 'facet3', 'facet4'], and
                        this facet_limits dict: {'facet1': 50, 'facet4': 10}, facet1 and facet4
                        would be limited to top 50 and 10 values respectively, whereas facet2 and
                        facet3 would be limited to the default of the top 10.
    :type facet_limits: a dict
    :param run_query: boolean value indicating whether the query should be run and the results
                      returned or whether the query should be created and the elasticsearch query
                      returned instead of the results. Defaults to True.
    :type run_query: boolean


    **Results:**

    The result of this action is a dictionary with the following keys:

    :rtype: A dict with the following keys
    :param fields: fields/columns and their extra metadata
    :type fields: list of dicts
    :param total: number of total matching records
    :type total: int
    :param records: list of matching results
    :type records: list of dicts
    :param facets: list of fields and their top 10 values, if requested
    :type facets: dict
    :param after: the next page's search_after value which can be passed back as the "after"
                  parameter. This value will always be included if there were results otherwise None
                  is returned. A value will also always be returned even if this page is the last.
    :type after: a list or None

    If run_query is True, then a dict with the following keys is returned instead:

    :param indexes: a list of the fully qualified indexes that the query would have been run against
    :type indexes: a list of strings
    :param search: the query dict that would have been sent to elasticsearch
    :type search: dict

    In addition to returning these result dicts, the actual result object is made available through
    the context dict under the key "versioned_datastore_query_result". This isn't available through
    the http action API however.
    '''
    original_data_dict, data_dict, version, search = create_search(context, data_dict)
    resource_id = data_dict[u'resource_id']

    # if the run query option is false (default to true if not present) then just return the query
    # we would have run against elasticsearch instead of actually running it. This is useful for
    # running the query outside of ckan, for example on a tile server.
    if not data_dict.get(u'run_query', True):
        # call pre_search to add all the versioning filters necessary (and other things too)
        result = utils.SEARCHER.pre_search(indexes=[resource_id], search=search, version=version)
        return {
            # the first part of the pre_search response is a list of indexes to run the query
            # against
            u'indexes': result[0],
            # the second part is the search object itself which we can call to_dict on to pull the
            # query out
            u'search': result[1].to_dict(),
        }
    else:
        try:
            # run the search through eevee. Note that we pass the indexes to eevee as a list as
            # eevee is ready for cross-resource search but this code isn't (yet)
            result = utils.SEARCHER.search(indexes=[resource_id], search=search, version=version)
        except NotFoundError as e:
            raise SearchIndexError(e.error)

        # allow other extensions implementing our interface to modify the result object
        for plugin in plugins.PluginImplementations(IVersionedDatastore):
            result = plugin.datastore_modify_result(context, original_data_dict, data_dict, result)

        # add the actual result object to the context in case the caller is an extension and they
        # have used one of the interface hooks to alter the search object and include, for example,
        # an aggregation
        context[u'versioned_datastore_query_result'] = result

        # get the fields
        mapping, fields = utils.get_fields(resource_id)
        # allow other extensions implementing our interface to modify the field definitions
        for plugin in plugins.PluginImplementations(IVersionedDatastore):
            fields = plugin.datastore_modify_fields(resource_id, mapping, fields)

        # return a dictionary containing the results and other details
        return {
            u'total': result.total,
            u'records': [hit.data for hit in result.results()],
            u'facets': utils.format_facets(result.aggregations),
            u'fields': fields,
            u'after': result.last_after,
            u'_backend': u'versioned-datastore',
        }


def datastore_create(context, data_dict):
    '''
    Adds a resource to the versioned datastore. This action doesn't take any data, it simply ensures
    any setup work is complete for the given resource in the search backend. To add data after
    creating a resource in the datastore, use the datastore_upsert action.

    :param resource_id: resource id of the resource
    :type resource_id: string

    **Results:**

    :returns: True if the datastore was initialised for this resource (or indeed if it was already
              initialised) and False if not. If False is returned this implies that the resource
              cannot be ingested into the datastore because the format is not supported
    :rtype: boolean
    '''
    data_dict = utils.validate(context, data_dict, schema.versioned_datastore_create_schema())
    plugins.toolkit.check_access(u'datastore_create', context, data_dict)

    resource_id = data_dict[u'resource_id']
    # lookup the resource dict
    resource = logic.get_action(u'resource_show')(context, {u'id': resource_id})
    # only create the index if the resource is ingestable
    if utils.is_ingestible(resource):
        # note that the version parameter doesn't matter when creating the index so we can safely
        # pass None
        utils.SEARCHER.ensure_index_exists(DatastoreIndex(utils.CONFIG, resource_id, None))
        return True
    return False


def datastore_upsert(context, data_dict):
    '''
    Upserts data into the datastore for the resource. The data can be provided in the data_dict
    using the key 'data' or, if data is not specified, the URL on the resource is used.

    :param resource_id: resource id of the resource
    :type resource_id: string
    :param version: the version to store the data under (optional, if not specified defaults to now)
    :type version: int
    :param index_action: the index action to take. This must be one of:
                            - skip: skips indexing altogether, this therefore allows the updating a
                                    resource's data across multiple requests. If this argument is
                                    used then the only way the newly ingested version will become
                                    visible in the index is if a final request is made with one of
                                    the other index_actions below.
                            - remove: before the data in the new version is indexed, the records
                                      that were not included in the version are flagged as deleted
                                      meaning they will not be indexed in the new version. Note that
                                      even if a record's data hasn't changed in the new version
                                      (i.e. the v1 record looks the same as the v2 record) it will
                                      not be deleted. This index action is intended to fulfill the
                                      requirements of a typical user uploading a csv to the site -
                                      they expect the indexed resource to contain the data in the
                                      uploaded csv and nothing else.
                            - retain: just index the records, regardless of whether they were
                                      updated in the last version or not. This action allows for
                                      indexing partial updates to the resources' data.
    :type index_action: string (optional, default remove)


    **Results:**

    :returns: details about the job that has been submitted to fulfill the upsert request.
    :rtype: dict

    '''
    # this comes through as junk if it's not removed before validating. This happens because the
    # data dict is flattened during validation, but why this happens is unclear.
    data = data_dict.get(u'records', None)
    data_dict = utils.validate(context, data_dict, schema.versioned_datastore_upsert_schema())
    plugins.toolkit.check_access(u'datastore_upsert', context, data_dict)

    resource_id = data_dict[u'resource_id']
    # these 3 parameters are all optional and have the defaults defined below
    version = data_dict.get(u'version', to_timestamp(datetime.now()))
    index_action = data_dict.get(u'index_action', u'remove')

    # check that the version is valid
    if not check_version_is_valid(resource_id, version):
        raise plugins.toolkit.ValidationError(u'The new version must be newer than current version')

    # ensure our custom queue exists
    utils.ensure_importing_queue_exists()
    # queue the job on our custom queue
    job = enqueue_job(import_resource_data, args=[resource_id, utils.CONFIG, version, index_action,
                                                  data], queue=u'importing')
    return {
        u'queued_at': job.enqueued_at.isoformat(),
        u'job_id': job.id,
    }


def datastore_delete(context, data_dict):
    '''
    Deletes the data in the datastore against the given resource_id. Note that this is achieved by
    dropping the entire mongo collection at the resource_id's value.

    :param resource_id: resource id of the resource
    :type resource_id: string
    '''
    data_dict = utils.validate(context, data_dict, schema.versioned_datastore_delete_schema())
    plugins.toolkit.check_access(u'datastore_delete', context, data_dict)

    resource_id = data_dict[u'resource_id']
    # remove all resource data from elasticsearch (the eevee function used below deletes the index,
    # the aliases and the status entry for this resource so that we don't have to)
    delete_index(utils.CONFIG, resource_id)
    # remove all resource data from mongo
    with get_mongo(utils.CONFIG, collection=resource_id) as mongo:
        mongo.drop()


@logic.side_effect_free
def datastore_get_record_versions(context, data_dict):
    '''
    Given a record id and an resource it appears in, returns the version timestamps available for
    that record in ascending order.

    Data dict params:
    :param resource_id: resource id that the record id appears in
    :type resource_id: string
    :param id: the id of the record
    :type id: integer

    **Results:**

    :returns: a list of versions
    :rtype: list
    '''
    data_dict = utils.validate(context, data_dict, schema.datastore_get_record_versions_schema())
    return utils.SEARCHER.get_versions(data_dict[u'resource_id'], int(data_dict[u'id']))


@logic.side_effect_free
def datastore_get_resource_versions(context, data_dict):
    '''
    Given a resource id, returns the version timestamps available for that resource in ascending
    order along with the number of records modified in the version.

    Data dict params:
    :param resource_id: resource id
    :type resource_id: string

    **Results:**

    :returns: a list of versions and the number of changes in the form {"version": #, "changes": #}
    :rtype: list of dicts
    '''
    data_dict = utils.validate(context, data_dict, schema.datastore_get_resource_versions_schema())
    resource_id = data_dict[u'resource_id']
    index = utils.SEARCHER.prefix_index(resource_id)

    after = None
    versions = []
    while True:
        search = Search(using=utils.SEARCHER.elasticsearch, index=index)[0:0]
        search.aggs.bucket(u'versions', u'composite', size=100,
                           sources={u'version': A(u'terms', field=u'meta.version', order=u'asc')})
        if after is not None:
            search.aggs[u'versions'].after = {u'version': after}

        result = search.execute().aggs.to_dict()[u'versions']
        for bucket in result[u'buckets']:
            versions.append({u'version': bucket[u'key'][u'version'],
                             u'changes': bucket[u'doc_count']})

        after = result.get(u'after_key', {}).get(u'version', None)
        if after is None:
            break

    return versions


@logic.side_effect_free
def datastore_autocomplete(context, data_dict):
    '''
    Provides autocompletion results against a specific field in a specific resource.

    **Data dict params:**

    :param resource_id: id of the resource to be searched against
    :type resource_id: string
    :param q: full text query. If a string is passed, all fields are searched with the value. If a
          dict is passed each of the fields and values contained within will be searched as
          required (e.g. {"field1": "a", "field2": "b"}).
    :type q: string or dictionary
    :param filters: a dictionary of conditions that must be met to match a record
                    (e.g {"field1": "a", "field2": "b"}) (optional)
    :type filters: dictionary
    :param limit: maximum number of records to return (optional, default: 100)
    :type limit: int
    :param after: search after offset value as a base64 encoded string
    :type after: string
    :param field: the field to autocomplete against
    :type field: string
    :param term: the search term for the autocompletion
    :type term: string
    :param version: version to search at, if not provided the current version of the data is
                   searched.
    :type version: int, number of milliseconds (not seconds!) since UNIX epoch


    **Results:**

    :returns: a dict containing the list of values and an after value for the next page's results
    :rtype: dict
    '''
    # ensure the data dict is valid against our autocomplete action schema
    data_dict = utils.validate(context, data_dict, schema.datastore_autocomplete_schema())

    # extract the fields specifically needed for setting up the autocomplete query
    field = data_dict.pop(u'field')
    term = data_dict.pop(u'term')
    after = data_dict.pop(u'after', None)
    # default to a size of 20 results
    size = data_dict.pop(u'limit', 20)
    # ensure the search doesn't respond with any hits cause we don't need them
    data_dict[u'limit'] = 0
    # remove the offset if one was passed as we don't need it
    data_dict.pop(u'offset', None)

    # now build the search object against the normal search code
    _original_data_dict, data_dict, version, search = create_search(context, data_dict)
    # get the resource id we're going to search against
    resource_id = data_dict[u'resource_id']

    # add the autocompletion query part which takes the form of a prefix search
    search = search.filter(u'prefix', **{prefix_field(field): term})
    # modify the search so that it has the aggregation required to get the autocompletion results
    search.aggs.bucket(u'field_values', u'composite', size=size,
                       sources={field: A(u'terms', field=prefix_field(field), order=u'asc')})
    # if there's an after included, add it into the aggregation
    if after:
        search.aggs[u'field_values'].after = {field: after}

    # run the search
    result = utils.SEARCHER.search(indexes=[resource_id], search=search, version=version)
    # get the results we're interested in
    agg_result = result.aggregations[u'field_values']
    # return a dict of results, but only include the after details if there are any to include
    return_dict = {
        u'values': [bucket[u'key'][field] for bucket in agg_result[u'buckets']],
    }
    if u'after_key' in agg_result:
        return_dict[u'after'] = agg_result[u'after_key'][field]
    return return_dict


def datastore_reindex(context, data_dict):
    '''
    Triggers a reindex of the given resource's data. This does not reingest the data to mongo, but
    it does reindex the data in mongo to elasticsearch. The intent of this action is to allow
    mapping changes (for example) to be picked up.

    Data dict params:
    :param resource_id: resource id that the record id appears in
    :type resource_id: string

    **Results:**

    :returns: a dict containing the details of the reindex as returned from elasticsearch
    :rtype: dict
    '''
    # validate the data dict
    data_dict = utils.validate(context, data_dict, schema.datastore_reindex())
    # check auth
    plugins.toolkit.check_access(u'datastore_reindex', context, data_dict)
    # retrieve the resource id
    resource_id = data_dict[u'resource_id']
    # retrieve the resource itself
    resource = logic.get_action(u'resource_show')(context, {u'id': resource_id})

    last_ingested_version = stats.get_last_ingest(resource_id)
    if last_ingested_version is None:
        raise plugins.toolkit.ValidationError(u'There is no ingested data for this version')

    job = enqueue_job(index_resource, args=[resource, utils.CONFIG, None, last_ingested_version],
                      queue=u'importing')
    return {
        u'queued_at': job.enqueued_at.isoformat(),
        u'job_id': job.id,
    }


@logic.side_effect_free
def datastore_query_extent(context, data_dict):
    '''
    Return the geospatial extent of the results of a given datastore search query. The data_dict
    parameters are the same as the arguments for `datastore_search`.


    **Results:**

    :rtype: A dict with the following keys
    :param total_count: total number of rows matching the query
    :type fields: integer
    :param geom_count: Number of rows matching the query that have geospatial information
    :type geom_count: int
    :param bounds: the extent of the query's results, this will be missing if no bound can be
                   calculated (for example, if the resource has no geo data)
    :type bounds: list in the format [[lat min, long min], [lat max, long max]]
    '''
    # ensure the search doesn't respond with any hits cause we don't need them and override two
    # unused params
    data_dict[u'limit'] = 0
    data_dict.pop(u'offset', None)
    data_dict.pop(u'after', None)

    # now build the search object against the normal search code
    _original_data_dict, data_dict, version, search = create_search(context, data_dict)
    # get the resource id we're going to search against
    resource_id = data_dict[u'resource_id']

    # add our bounds and geo count aggregations
    search.aggs.bucket(u'bounds', u'geo_bounds', field=u'meta.geo', wrap_longitude=False)
    search.aggs.bucket(u'geo_count', u'value_count', field=u'meta.geo')

    # run the search
    result = utils.SEARCHER.search(indexes=[resource_id], search=search, version=version)

    # create a dict of results for return
    to_return = {
        u'total_count': result.hits.total,
        u'geom_count': result.aggregations[u'geo_count'][u'value'],
    }

    # extract and add the bounds info from the aggregations if there is any
    if result.aggregations[u'geo_count'][u'value'] > 0:
        top_left = result.aggregations[u'bounds'][u'bounds'][u'top_left']
        bottom_right = result.aggregations[u'bounds'][u'bounds'][u'bottom_right']
        to_return[u'bounds'] = [[p[u'lat'], p[u'lon']] for p in (top_left, bottom_right)]

    return to_return
