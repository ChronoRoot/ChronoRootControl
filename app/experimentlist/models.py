import os
from datetime import datetime
from app.experiment.models import Experiment
from config import Config
from flask import current_app as app

class ExperimentList(object):
    """
    Handle the information of the experiments of the directory
    """
    exps = []

    def __init__(self):
        """
        Initialisation of the experiment list
        """
        self.load()

    def load(self):
        """
        Load the information of the experiment list from the working directory
        """
        self.exps = []
        directory = Config.WORKING_DIR
        
        if not os.path.exists(directory):
            return

        # Iterate over folder names (which are the IDs)
        for folder_name in os.listdir(directory):
            
            # 1. Skip hidden files (like .DS_Store) or system folders
            if folder_name.startswith('.'): 
                continue
            
            # 2. Verify it is a directory
            full_path = os.path.join(directory, folder_name)
            if not os.path.isdir(full_path):
                continue

            try:
                exp = Experiment.load_from_id(folder_name)
                self.exps.append(exp)
                
            except Exception as e:
                # Catch errors (like missing info.json) so one bad folder doesn't crash the app
                app.logger.error(f"Skipping invalid experiment folder '{folder_name}': {e}")
                continue

    def to_dict(self):
        """
        Convert to dict for serialisation
        """
        return {exp.expid: exp.to_dict() for exp in self.exps}

    def find_conflict(self, new_start, new_end, ignore_id=None):
        """
        Check for scheduling conflicts.
        """
        # Parse inputs strings into datetime objects
        # We assume the input format matches the one defined in Experiment class
        try:
            if isinstance(new_start, str):
                s = datetime.strptime(new_start, '%Y-%m-%d %H:%M:%S')
            else:
                s = new_start

            if isinstance(new_end, str):
                e = datetime.strptime(new_end, '%Y-%m-%d %H:%M:%S')
            else:
                e = new_end
        except ValueError:
            # If parsing fails, we cannot determine conflict
            app.logger.error(f"Invalid date format in conflict check: {new_start} / {new_end}")
            return None

        for exp in self.exps:
            # Ignore inactive experiments
            if exp.status in ['FINISHED', 'CANCELLED']:
                continue
            
            # Ignore self
            if ignore_id and str(exp.expid) == str(ignore_id):
                continue

            # Check Intersection
            # exp.start and exp.end are now datetime properties
            if (s < exp.end) and (e > exp.start):
                return exp
        
        return None
    
    @classmethod
    def get_archived_history(cls):
        """
        Scans the working directory and returns cleanly formatted archived experiments.
        Does not instantiate full Experiment objects for maximum performance.
        """
        directory = Config.WORKING_DIR
        archived_data = {}
        
        if not os.path.exists(directory):
            return archived_data

        for folder_name in os.listdir(directory):
            if folder_name.startswith('.'): 
                continue
            
            full_path = os.path.join(directory, folder_name)
            if not os.path.isdir(full_path):
                continue

            # Fetch the lightweight summary
            summary = Experiment.get_archived_summary(folder_name)
            if summary:
                archived_data[folder_name] = summary
                
        return archived_data