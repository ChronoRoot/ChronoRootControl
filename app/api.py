"""
The API definitions
"""

import os
from flask import Blueprint, abort, jsonify, make_response, request

from app.experiment.models import Experiment
from app.experimentlist.models import ExperimentList
from config import Config

api_exp = Blueprint('api_exp', __name__, template_folder='templates',
                    static_folder='static')


@api_exp.route('', methods=['GET'])
def get_experiment_list():
    """
    Return the full list of experiments
    """
    exps = ExperimentList()
    return jsonify(exps.to_dict())


@api_exp.route('', methods=['POST'])
def create_experiment():
    """
    Create an experiment

    required fields:
        start, end, timepoint_nb and camera
    """
    if (not request.json or
            'start' not in request.json or
            'end' not in request.json or
            'timepoint_nb' not in request.json or
            'cameras' not in request.json):
        abort(400)
    exp = Experiment()
    exp.from_dict(request.json)
    exp.create()
    return jsonify(exp.to_dict()), 201


# Return the json of an experiment
@api_exp.route('/<expid>', methods=['GET'])
def get_experiment(expid):
    """
    Return an experiment

    Args:
      expid : str
        The id of the experiment
    """
    try:
        exp = Experiment(directory=os.path.join(Config.WORKING_DIR, expid))
    except FileNotFoundError:
        abort(404)
    return jsonify(exp.to_dict())


@api_exp.errorhandler(404)
def not_found(error):
    return make_response(jsonify({'error': 'Not found'}), 404)


@api_exp.route('/<expid>', methods=['DELETE'])
def delete_experiment(expid):
    """
    Delete an experiment

    Args:
      expid : str
        The id of the experiment
    """
    try:
        exp = Experiment(directory=os.path.join(Config.WORKING_DIR, expid))
    except FileNotFoundError:
        abort(404)
    exp.delete()
    return jsonify({'result': True})


@api_exp.route('/<expid>', methods=['PUT'])
def update_experiment(expid):
    """
    Update an experiment

    Args:
      expid : str
        The id of the experiment
    """
    try:
        exp = Experiment(directory=os.path.join(Config.WORKING_DIR, expid))
    except FileNotFoundError:
        abort(404)
    exp.from_dict(request.json)
    exp.create()
    return jsonify(exp.to_dict())


@api_exp.route('/<expid>/cancel', methods=['GET'])
def cancel_experiment(expid):
    """
    Cancel an experiment

    Args:
      expid : str
        The id of the experiment
    """
    try:
        exp = Experiment(directory=os.path.join(Config.WORKING_DIR, expid))
    except FileNotFoundError:
        abort(404)
    exp.cancel()
    return jsonify(exp.to_dict())


@api_exp.route('/diagnostic', methods=['POST'])
def diagnostic():
    """
    Triggers the diagnostic using the 'system' experiment profile
    """
    try:
        # We use 'system' as a reserved ID for hardware-wide checks
        exp = Experiment(directory=os.path.join(Config.WORKING_DIR, 'system'))
    except FileNotFoundError:
        # Create it if it doesn't exist
        exp = Experiment(expid='system')
    
    exp.diagnostic()
    return jsonify({'result': True, 'expid': 'system'})

from flask import send_from_directory

@api_exp.route('/system_images/<path:filename>', methods=['GET'])
def get_system_image(filename):
    """
    Serves diagnostic images from the working directory.
    Example filename: 'system/1/2026-02-03_camera_1.png'
    """
    # We split the filename because send_from_directory expects the base folder 
    # and the relative filename separately.
    # Config.WORKING_DIR is where 'system' folder lives.
    return send_from_directory(Config.WORKING_DIR, filename)

@api_exp.route('/reboot', methods=['POST'])
def reboot():
    """
    Triggers the reboot of the module
    """
    try:
        # Schedule the reboot in 1 second to allow the API to send the response back first
        # using 'shutdown -r' is safer than 'reboot'
        os.system('(sleep 1; sudo shutdown -r now) &')
        return jsonify({'result': True, 'message': 'System is rebooting...'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500