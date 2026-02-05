"""
The API definitions
"""

import os
from flask import Blueprint, abort, jsonify, make_response, request, send_from_directory

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
    Create and Launch an experiment via API.
    """
    if not request.json:
        abort(400, description="Missing JSON body")

    required = ['start', 'end', 'cameras']
    if not all(k in request.json for k in required):
        abort(400, description=f"Missing required fields: {required}")

    # 1. Initialize Blank Experiment (Status = SETUP)
    exp = Experiment()
    exp.from_dict(request.json)
    xpid = exp.xpid

    # 2. Scheduling Validation
    exp_list = ExperimentList()
    
    conflict = exp_list.find_conflict(exp.start, exp.end)
    if conflict:
        return make_response(jsonify({
            'error': 'Scheduling Conflict', 
            'message': f"Overlaps with experiment {conflict.expid}"
        }), 409)

    # 3. Launch (Transitions SETUP -> NEW -> Saves -> Notifies Mule)
    exp.create()
    
    return jsonify(exp.to_dict()), 201


@api_exp.route('/<expid>', methods=['GET'])
def get_experiment(expid):
    """
    Return an experiment by ID
    """
    try:
        # USE NEW FACTORY METHOD
        exp = Experiment.load_from_id(expid)
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
    """
    try:
        exp = Experiment.load_from_id(expid)
    except FileNotFoundError:
        abort(404)
    exp.delete()
    return jsonify({'result': True})


@api_exp.route('/<expid>', methods=['PUT'])
def update_experiment(expid):
    """
    Update an experiment
    """
    try:
        exp = Experiment.load_from_id(expid)
    except FileNotFoundError:
        abort(404)
    
    # Update fields
    exp.from_dict(request.json)

    # SAFETY: Check for conflicts (ignoring itself)
    exp_list = ExperimentList()
    conflict = exp_list.find_conflict(exp.start, exp.end, ignore_id=exp.expid)
    
    if conflict:
        return make_response(jsonify({
            'error': 'Scheduling Conflict', 
            'message': f"Overlaps with experiment {conflict.expid}"
        }), 409)

    exp.create() # Saves and updates scheduler
    return jsonify(exp.to_dict())


@api_exp.route('/<expid>/cancel', methods=['GET', 'POST']) 
def cancel_experiment(expid):
    """
    Cancel an experiment. Accepts GET or POST.
    """
    try:
        exp = Experiment.load_from_id(expid)
    except FileNotFoundError:
        abort(404)
    exp.cancel()
    return jsonify(exp.to_dict())


@api_exp.route('/diagnostic', methods=['POST'])
def diagnostic():
    """
    Triggers the diagnostic using the 'system' experiment profile
    """
    # 1. Create a transient experiment object for system tasks
    exp = Experiment()
    exp.expid = 'system'
    exp.workdir = os.path.join(Config.WORKING_DIR, 'system')
    
    # Ensure directory exists (create manually since we aren't calling save())
    if not os.path.exists(exp.workdir):
        os.makedirs(exp.workdir)

    exp.diagnostic()
    return jsonify({'result': True, 'expid': 'system'})


@api_exp.route('/system_images/<path:filename>', methods=['GET'])
def get_system_image(filename):
    """
    Serves diagnostic images.
    """
    return send_from_directory(Config.WORKING_DIR, filename)


@api_exp.route('/reboot', methods=['POST'])
def reboot():
    """
    Triggers the reboot of the module
    """
    try:
        os.system('(sleep 1; sudo shutdown -r now) &')
        return jsonify({'result': True, 'message': 'System is rebooting...'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500