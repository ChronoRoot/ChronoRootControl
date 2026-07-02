from flask_wtf import FlaskForm as Form
from wtforms import BooleanField, SelectField, StringField, IntegerField, PasswordField, widgets
from wtforms.validators import Optional

class SyncSettingsForm(Form):
    sync_enabled = BooleanField("Enable Automated Background Sync", default=False)
    sync_interval = IntegerField("Sync Interval (Minutes)", default=60)
    
    remote_type = SelectField("Connection Type", choices=[
        ('local', 'Local'), 
        ('sftp', 'SSH / SFTP (Linux Server)'),
        ('ftp', 'FTP Server'),
        ('advanced', 'Advanced Cloud (Google Drive, AWS, etc.)')
    ])
    
    host = StringField("Host IP / Address")
    port = IntegerField("Port", validators=[Optional()])
    user = StringField("Username")
    
    # FIXED: Pass hide_value to the widget itself for WTForms 3.x compatibility
    password = PasswordField(
        "Password", 
        widget=widgets.PasswordInput(hide_value=False),
        render_kw={"placeholder": "********"}
    )
    
    destination_path = StringField("Destination Folder / Path")