import time
import openai
import json
import os
import re
from flask import Flask, request, jsonify
import pandas as pd
import boto3
from botocore.exceptions import ClientError
import threading
from threading import Event

app = Flask(__name__)

# Set up AWS Secrets Manager
secret_name = "openai_api_key"
region_name = "eu-central-1"

# Create a Secrets Manager client
session = boto3.session.Session()
client = session.client(
    service_name='secretsmanager',
    region_name=region_name
)

def get_openai_api_key():
    try:
        get_secret_value_response = client.get_secret_value(SecretId=secret_name)
        secret = get_secret_value_response['SecretString']
        secret_dict = json.loads(secret)
        api_key = secret_dict.get('OPENAI_API_KEY')
        if not api_key:
            raise ValueError("OPENAI_API_KEY is not found in the secret.")
        return api_key
    except ClientError as e:
        raise e

api_key = get_openai_api_key()
openai.api_key = api_key

# Determine the folder where the script is located
base_dir = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(base_dir, 'uploads', 'docs')
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'xlsx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure the upload directory exists
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def format_message_content(content):
    if not isinstance(content, str):
        content = str(content)
    content = re.sub(r'###### (.*?)\n', r'<h6>\1</h6>', content)
    content = re.sub(r'##### (.*?)\n', r'<h5>\1</h5>', content)
    content = re.sub(r'#### (.*?)\n', r'<h4>\1</h4>', content)
    content = re.sub(r'### (.*?)\n', r'<h3>\1</h3>', content)
    content = re.sub(r'## (.*?)\n', r'<h2>\1</h2>', content)
    content = re.sub(r'# (.*?)\n', r'<h1>\1</h1>', content)
    content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', content)
    content = content.replace('\n', '<br>')
    return content

@app.route('/productive_broker_analyze', methods=['POST'])
def productive_broker_analyze():
    path = request.json.get('path')
    
    if not path:
        return jsonify({"error": "Path parameter is required"}), 400
    
    # handelt langlaufende Aufgaben ohne direkten Timeout
    task_completed = Event()

    def analyze_task():
        try:
            data = pd.read_excel(path, engine='openpyxl')
            result_json = data.to_json(orient='records', indent=4)

            temp_thread_id = create_thread("Ermittle die Definition für produktive Makler entsprechend deiner Knowledge Base.")
            print(f"Thread 1 started: {temp_thread_id}")
            
            run1 = client.beta.threads.runs.create_and_poll(thread_id=temp_thread_id, assistant_id=assistant.id)
            print(f"Run 1 status: {run1.status}")

            if run1.status == 'completed':
                print("Run 1 completed successfully")

                thread_message = client.beta.threads.messages.create(
                            temp_thread_id,
                            role="user",
                            content=(
                                f'Wende diese Definition auf die folgenden Maklervertrieb Zahlen an und sage mir welche Makler entsprechend dieser Definition produktiv sind: {result_json}'
                            ),
                        )

                run2 = client.beta.threads.runs.create_and_poll(thread_id=temp_thread_id, assistant_id=assistant.id)
                print(f"Run 2 status: {run2.status}")
                
                if run2.status == 'completed':
                    print("Run 2 completed successfully")

                    messages = list(client.beta.threads.messages.list(thread_id=temp_thread_id))
                    response = process_messages(messages)

            task_completed.set()
            return jsonify({"data": response})
        except Exception as e:
            print(f"An error occurred: {str(e)}")
            task_completed.set()
            return jsonify({"error": f"An error occurred: {str(e)}"}), 500

    app.logger.info('Starting Threaded Task')
    threading.Thread(target=analyze_task).start()

    # Wait for task completion or a defined period
    task_success = task_completed.wait(timeout=300)  # extends HTTP Timeout für langlaufende Task

    if not task_success:
        return jsonify({
            "status": "Task is still running. Please check back later."
        }), 202

    return jsonify({
        "status": "Task completed."
    })

def create_thread(content_user_input):
    thread = client.beta.threads.create()
    message = client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=content_user_input,
    )
    return thread.id

def process_messages(messages):
    response = []
    for message in messages:
        if hasattr(message, 'content'):
            if isinstance(message.content, list):
                for content_item in message.content:
                    if hasattr(content_item, 'text') or hasattr(content_item, 'value'):
                        formatted_content = format_message_content(content_item)
                        response.append({"role": "assistant", "content": formatted_content})
                        break  # Handle only the first relevant content
            else:
                formatted_content = format_message_content(message.content)
                response.append({"role": "assistant", "content": formatted_content})
    return response

if __name__ == '__main__':
    # Running the application facilities
    app.run(host='0.0.0.0', port=8080, threaded=True, use_reloader=False)