import os
import markdown
from flask import Blueprint, render_template, abort
from markupsafe import Markup

help_page = Blueprint('help_page', __name__)

@help_page.route('/')
@help_page.route('/<section>')
def index(section='about'):
    # Mapping of URL segments to the physical markdown files
    sections = {
        'about': '01_about.md',
        'manual': '02_manual.md',
        'install': '03_install.md',
        'configuration': '04_configuration.md',
        'architecture': '05_architecture.md',
        'api': '06_api.md',
    }

    if section not in sections:
        abort(404)

    # __file__ is app/doc/__init__.py, so its dirname is the app/doc/ folder.
    current_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(current_dir, sections[section])
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            raw_markdown = f.read()
            
        # Convert markdown to HTML (enabling tables and fenced code blocks)
        parsed_html = markdown.markdown(raw_markdown, extensions=['tables', 'fenced_code'])
        
        # Wrap in Markup to safely render HTML
        html_content = Markup(parsed_html)
        
    except FileNotFoundError:
        # Failsafe: Prints exactly where it looked so you can debug typos
        html_content = Markup(f"<div class='alert alert-danger'>Documentation file not found at: <br><code>{file_path}</code></div>")
    except Exception as e:
        html_content = Markup(f"<div class='alert alert-danger'>Error loading docs: {str(e)}</div>")

    return render_template('help.html', content=html_content, current_section=section)