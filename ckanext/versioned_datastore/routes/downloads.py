from ckan.plugins import toolkit
from flask import Blueprint, send_from_directory

blueprint = Blueprint(name=u'downloads', import_name=__name__)


@blueprint.route(u'/downloads/<zip_name>')
def download(zip_name):
    '''
    Serves up the requested zip from the download directory. This is only registered with flask when
    running in debug mode and therefore is only for testing in development. In production we should
    always serve files through the web server.

    :param zip_name: the zip name
    :return: the send file response
    '''
    download_dir = toolkit.config.get(u'ckanext.versioned_datastore.download_dir')
    return send_from_directory(download_dir, zip_name)
