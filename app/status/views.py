from flask import jsonify, current_app

from . import status
from . import utils


@status.route('/_status')
def status():
    return jsonify(status="ok", version=utils.get_version_label())