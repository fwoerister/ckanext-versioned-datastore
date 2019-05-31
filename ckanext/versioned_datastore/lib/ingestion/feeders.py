import itertools
import logging
import numbers
from contextlib import closing

import abc
import openpyxl
import requests
import six
import unicodecsv as csv
import xlrd
from cchardet import UniversalDetector
from eevee.ingestion.feeders import IngestionFeeder
from openpyxl.cell.read_only import EmptyCell

from ckanext.versioned_datastore.lib.ingestion.records import DatastoreRecord
from ckanext.versioned_datastore.lib.utils import download_to_temp_file


# TODO: use tabulator lib: https://github.com/frictionlessdata/tabulator-py. We currently can't use
#       this because it's sqlalchemy requirements clash with those of our old version of ckan. Once
#       we've completed the upgrade to ckan 2.8.x we should switch this code out of tabulator
#       (unless we have a compelling reason not to)

log = logging.getLogger(__name__)


@six.add_metaclass(abc.ABCMeta)
class DatastoreFeeder(IngestionFeeder):
    '''
    Base abstract class for the datastore feeders.
    '''

    def __init__(self, version, resource_id, id_offset):
        '''
        :param version: the version of the data we're going to read
        :param resource_id: the resource id
        '''
        super(DatastoreFeeder, self).__init__(version)
        self.resource_id = resource_id
        self.id_offset = id_offset

        # keep track of the columns we've seen
        self.all_columns = set()
        self.columns = []

    def update_columns(self, columns):
        """
        Update the list of columns we've seen.

        :param columns: a sequence of columns
        """
        for column in columns:
            if column != u'_id' and column not in self.all_columns:
                self.all_columns.add(column)
                self.columns.append(column)

    def create_record(self, number, data):
        '''
        Creates a record given the row number of the record (1-based) and the data from that row as
        a dict.

        :param number: the row number (1-based of the record)
        :param data: the row's data as a dictionary
        :return: a new DatastoreRecord object
        '''
        # update the headers
        self.update_columns(data.keys())
        # if the record has an _id column then we use it, if it doesn't then we just use the index
        # of the record in the source plus the offset value. This accommodates the simple scenario
        # where the source data dicts don't have ids and the user just wants to add to the existing
        record_id = int(data.pop(u'_id', self.id_offset + number))
        return DatastoreRecord(self.version, record_id, data, self.resource_id)


@six.add_metaclass(abc.ABCMeta)
class URLDatastoreFeeder(DatastoreFeeder):
    '''
    Abstract class encapsulating data that comes from a URL.
    '''

    def __init__(self, version, resource_id, id_offset, url, api_key, is_upload):
        '''
        :param url: the url where the data resides
        :param api_key: the API key of a user who can read the data, if indeed the data needs an API
                        key to get it. This is needed when the URL is the CKAN resource download URL
                        of a private resource. Can be None to indicate no API key is required
        :param is_upload: whether the URL we're going to read from is a CKAN upload URL
        '''
        super(URLDatastoreFeeder, self).__init__(version, resource_id, id_offset)
        self.url = url
        self.api_key = api_key
        self.is_upload = is_upload

    @property
    def headers(self):
        '''
        Returns the headers to be used when retrieving the data from the URL. By default this is an
        empty dict if there is no authentication required, otherwise we include the provided API
        key.

        :return: a dict of headers
        '''
        return {u'Authorization': self.api_key} if self.is_upload and self.api_key else {}

    @property
    def source(self):
        '''
        Where the data we've read came from. We just return the url.

        :return: the url from which the data is collected and returned in the form of records
        '''
        return self.url


class APIDatastoreFeeder(DatastoreFeeder):
    '''
    Feeder for API uploaded data. This is data that doesn't have a URL source, it has just been
    POSTed to the server as a JSON list.
    '''

    def __init__(self, version, resource_id, id_offset, data):
        super(APIDatastoreFeeder, self).__init__(version, resource_id, id_offset)
        self.data = data

    @property
    def source(self):
        '''
        Where the data we've read came from. We just return the string "API".

        :return: always "API"
        '''
        return u'API'

    def records(self):
        '''
        Returns a generator of DatastoreRecord objects. Given that the data is already a list of
        dicts, we just iterate through them directly.

        :return: a generator which yields DatastoreRecord objects
        '''
        return itertools.starmap(self.create_record, enumerate(self.data, start=1))


class SVFeeder(URLDatastoreFeeder):
    '''
    Separated Value feeder, this feeder reads data from a URL and then parses it using the given
    dialect with the unicodecsv DictReader.
    '''

    def __init__(self, version, resource_id, id_offset, url, api_key, is_upload, dialect):
        '''
        :param version: the version of the data to be fed
        :param resource_id: the resource id of the data's resource
        :param id_offset: the id offset value. This is used if the data to be fed should be added to
                          the existing data in the resource, i.e. the ids already used should be
                          accounted for. For example, say in version 1, 5 records exist with ids
                          1-5. In version 2, another 3 records are added that shouldn't replace the
                          existing ones, this id offset value is used to start the ids at the given
                          value, for example in this case, 6.
        :param url: the URL to read the data from. This URL will be hit twice, firstly to work out
                    the encoding of the data and then to read it.
        :param api_key: the API key of a user who can read the data, if indeed the data needs an API
                        key to get it. This is needed when the URL is the CKAN resource download URL
                        of a private resource. Can be None to indicate no API key is required.
        :param is_upload: whether the URL we're going to read from is a CKAN upload URL
        :param dialect: the dialect to pass to the csv.DictReader init function as the dialect
                        parameter
        '''
        super(SVFeeder, self).__init__(version, resource_id, id_offset, url, api_key, is_upload)
        self.dialect = dialect

    def records(self):
        '''
        Generator of records from the *SV file. This function will read a small sample of rows from
        the source in an attempt to determine character encoding before reading the entire source
        and yielding the records.

        :return: a generator of records
        '''
        detector = UniversalDetector()
        # number of lines to sample from the source
        sample_size = 400
        with closing(requests.get(self.url, stream=True, headers=self.headers)) as response:
            # make sure we get a response we can use
            response.raise_for_status()
            for line_number, line in enumerate(response.iter_lines(), start=1):
                detector.feed(line)
                # stop if we've read enough lines for the detector to be confident about the
                # encoding or if we've reached the sample size limit
                if detector.done or line_number == sample_size:
                    break
            detector.close()

        encoding = detector.result[u'encoding']
        # if the detector failed to work out the encoding (unlikely) or if the encoding it comes up
        # with is ASCII, just default to UTF-8. This is worth doing for ASCII detections as utf-8
        # is a superset of ASCII so by switching to utf-8 we're more likely to catch non-ASCII
        # characters in the source after the sample size limit.
        if encoding is None or encoding == u'ASCII':
            encoding = u'utf-8'

        log.info(u'Using encoding {} for resource {} (version: {})'.format(encoding,
                                                                           self.resource_id,
                                                                           self.version))

        # stream the file from the url - note that we have to use closing here because the ability
        # to directly use with on requests.get wasn't added until 2.18.0 and we're on 2.10.0 :(
        with closing(requests.get(self.url, stream=True, headers=self.headers)) as response:
            # make sure we get a response we can use
            response.raise_for_status()
            reader = csv.DictReader(response.iter_lines(), dialect=self.dialect, encoding=encoding)

            # if there are columns, use them, then we are sure to get the right order. create_record
            # will call update_columns too but if we get there first the calls won't do anything as
            # all the columns it attempts to add will have already been seen
            if reader.unicode_fieldnames:
                self.update_columns(reader.unicode_fieldnames)

            for number, data in enumerate(reader, start=1):
                # yield a new record for each row
                yield self.create_record(number, data)


class CSVFeeder(SVFeeder):
    '''
    Feeds records from a CSV.
    '''

    def __init__(self, version, resource_id, id_offset, url, api_key, is_upload):
        super(CSVFeeder, self).__init__(version, resource_id, id_offset, url, api_key,
                                        is_upload, u'excel')


class TSVFeeder(SVFeeder):
    '''
    Feeds records from a TSV.
    '''

    def __init__(self, version, resource_id, id_offset, url, api_key, is_upload):
        super(TSVFeeder, self).__init__(version, resource_id, id_offset, url, api_key,
                                        is_upload, u'excel-tab')


class XLSFeeder(URLDatastoreFeeder):
    '''
    Feeds records from an XLS (old excel) file.
    '''

    def records(self):
        '''
        Generator of records from (old) excel files. This function reads the excel file in its
        entirety at the start of processing and can't stream it because (old) excel files can't be
        streamed :(

        :return: a generator of records
        '''
        # download the url into a temporary file and then read from that. This is necessary as xls
        # files can't be streamed, they have to be completed loaded into memory
        with download_to_temp_file(self.url, self.headers) as temp:
            # open the xls file up
            book = xlrd.open_workbook(temp.name)
            # select the first sheet by default
            sheet = book.sheet_by_index(0)
            # get a row generator
            rows = sheet.get_rows()
            # assume the first row is the header
            header = [unicode(cell.value) for cell in next(rows)]
            # then read all the other rows as data
            for number, row in enumerate(rows, start=1):
                data = {}
                for field, cell in zip(header, row):
                    # if the cell is the id column, it contains a number and the number is an
                    # integer, convert it from a float to an int
                    if (field == u'_id' and cell.ctype == xlrd.XL_CELL_NUMBER and
                            cell.value.is_integer()):
                        data[field] = int(cell.value)
                    elif cell == xlrd.XL_CELL_EMPTY:
                        # ignore empty cells
                        continue
                    else:
                        # otherwise just use the value
                        data[field] = unicode(cell.value)
                # yield a new record
                yield self.create_record(number, data)


class XLSXFeeder(URLDatastoreFeeder):
    '''
    Feeds records from an XLSX (new excel) file.
    '''

    def records(self):
        '''
        Generator of records from (new) excel files. This function reads the excel file in its
        entirety at the start of processing and can't stream it because (new) excel files can't be
        streamed :(

        :return: a generator of records
        '''
        # download the url into a temporary file and then read from that. This is necessary as xlsx
        # files can't be streamed, they have to be completed loaded into memory
        with download_to_temp_file(self.url, self.headers) as temp:
            wb = openpyxl.load_workbook(temp, read_only=True)
            # get a generator for the rows in the active workbook
            rows = wb.active.rows
            # always treat the first row as a header
            header = [unicode(cell.value) for cell in next(rows)]
            # then read all the other rows as data
            for number, row in enumerate(rows, start=1):
                data = {}
                for field, cell in zip(header, row):
                    # if the cell is the id column and it contains a number make sure it stays a
                    # number
                    if field == u'_id' and isinstance(cell.value, numbers.Number):
                        data[field] = cell.value
                    # ignore empty cells
                    elif isinstance(cell, EmptyCell):
                        continue
                    else:
                        # convert everything else to unicode
                        data[field] = unicode(cell.value)
                # yield a new record
                yield self.create_record(number, data)
