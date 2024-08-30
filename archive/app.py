from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
import os
import openai
import pandas as pd
import re
import json
import boto3
from botocore.exceptions import ClientError
import threading
from threading import Event
import logging

secret_name = "openai_api_key"
region_name = "eu-central-1"

# Create a Secrets Manager client
session = boto3.session.Session()
client = session.client(
    service_name='secretsmanager',
    region_name=region_name
)

try:
    get_secret_value_response = client.get_secret_value(
        SecretId=secret_name
    )
except ClientError as e:
    # For a list of exceptions thrown, see
    # https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html
    raise e

secret = get_secret_value_response['SecretString']
secret_dict = json.loads(secret)

# Initialize OpenAI client
#api_key = os.getenv('OPENAI_API_KEY')
api_key = secret_dict.get('OPENAI_API_KEY')
if not api_key:
    raise Exception("OPENAI_API_KEY is not set in the environment.")
client = openai.OpenAI(api_key=api_key)

app = Flask(__name__)

# Determine the folder where the script is located
base_dir = os.path.dirname(os.path.abspath(__file__))

# Configure the upload folder relative to the script's directory
UPLOAD_FOLDER = os.path.join(base_dir, 'uploads', 'docs')
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'xlsx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

task_completed = Event()
analysis_result = {}

# Konfiguration für Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ensure the upload directory exists
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def format_message_content(content):
    if not isinstance(content, str):
        content = str(content)
    # Replace headers
    content = re.sub(r'###### (.*?)\n', r'<h6>\1</h6>', content)
    content = re.sub(r'##### (.*?)\n', r'<h5>\1</h5>', content)
    content = re.sub(r'#### (.*?)\n', r'<h4>\1</h4>', content)
    content = re.sub(r'### (.*?)\n', r'<h3>\1</h3>', content)
    content = re.sub(r'## (.*?)\n', r'<h2>\1</h2>', content)
    content = re.sub(r'# (.*?)\n', r'<h1>\1</h1>', content)
    # Bold text
    content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', content)
    # Convert new lines to <br>
    content = content.replace('\n', '<br>')
    return content

# OpenAI Assistant Configuration
function_calling_tool = [
    {
        "type": "function",
        "function": {
            "name": "target_analyze",
            "description": "Get the performance of the portfolio of brokers in terms of quantitative targets achieved."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "team_analyze",
            "description": "Get the current performance of the team and evaluate each member's statistics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "TeamID": {
                        "type": "string",
                        "description": "The unique identifier of the team, e.g., TE12345"
                    }
                },
                "required": ["TeamID"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_appointment_task",
            "description": "searches a free Appointment time in the calender for a broker meeting (Maklergespräch).",
            "parameters": {
                "type": "object",
                "properties": {
                    "brokerID": {
                        "type": "string",
                        "description": "The unique identifier of the broker, e.g., BR12345"
                    }
                },
                "required": ["brokerID"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "productive_broker_analyze",
            "description": "Get an overview of the current productive brokers."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_appointment",
            "description": "Create an appointment for a broker meeting (Maklergespräch).",
            "parameters": {
                "type": "object",
                "properties": {
                    "freeslot": {
                        "type": "string",
                        "description": "A time slot when the broker is available."
                    }
                },
                "required": ["freeslot"]
            }
        }
    }
]

file_search_tool = {
    "type": "file_search"
}

assistant = None
thread = None

def initialize_resources():
    global assistant

    if assistant is None:
        assistant = create_assistant(client, function_calling_tool, file_search_tool)
        logger.info(f"Assistant created with ID: {assistant.id}")
        file_paths_bucket = [os.path.join(base_dir, 'uploads', 'docs', filename) for filename in ['Input_1_sales.pdf', 'Input_4_Makler_Telefonleitfaden.pdf', 'Input_3_Leistungsabfall_roadmap.pdf', 'Zieldefinition MV v1.pdf']]
        create_data_base(file_paths_bucket, assistant.id)

def create_assistant(client, function_calling_tool, file_search_tool):
    global assistant
    
    assistant = client.beta.assistants.create(
        name="Broker Assistant",
        instructions=(
            "You are an expert performance advisor helping an account manager manage the performance of his insurance broker accounts. "
            "Use your knowledge base to answer questions and refer to the sources from your knowledge base you used to answer the question in your response. "
            "Give your answers in german."
        ),
        model="gpt-4o-mini",
        tools=[file_search_tool] + function_calling_tool
    )
    return assistant

def create_data_base(file_paths_bucket, assistant_id):
    vector_store = client.beta.vector_stores.create(name="Broker Assistant")
    
    file_streams = [open(path, "rb") for path in file_paths_bucket]

    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
        vector_store_id=vector_store.id, files=file_streams
    )
    
    client.beta.assistants.update(
        assistant_id=assistant_id,
        tool_resources={"file_search": {"vector_store_ids": [vector_store.id]}},
    )
    logger.info(f'File batch status: {file_batch.status}')
    logger.info(f'File batch file count: {file_batch.file_counts}')

def soll_ist_analyze(broker_number, file_path):
    df = pd.read_excel(file_path, engine='openpyxl')
    broker_data = df.loc[df['BrokerID'] == int(broker_number)]
    if broker_data.empty:
        return f"No data found for broker number: {broker_number}"
    grouped_data = broker_data.groupby(['Sparte', 'Produkt']).agg({
        'Target_1': 'sum',
        'Target_2': 'sum',
        'Target_3': 'sum',
        'KPI_1': 'sum',
        'KPI_2': 'sum',
        'KPI_3': 'sum'
    }).reset_index()
    performance_list = []
    for index, row in grouped_data.iterrows():
        division = row['Sparte']
        product = row['Produkt']
        targets = {
            "Target_1": row['Target_1'],
            "Target_2": row['Target_2'],
            "Target_3": row['Target_3']
        }
        kpis = {
            "KPI_1": row['KPI_1'],
            "KPI_2": row['KPI_2'],
            "KPI_3": row['KPI_3']
        }
        performance = {
            "Division": division,
            "Product": product,
            "Targets": targets,
            "Achievements": kpis
        }
        performance_list.append(performance)
    return performance_list

def target_analyze(file_path):
    # Einlesen der Excel-Datei
    logger.info('target_analyze function triggered')
    data = pd.read_excel(file_path, engine='openpyxl')
    result_json = data.to_json(orient='records', indent=4)
    
    output_template = {
            "Im Folgenden findest Du eine aktuelle Auflistung:"
            "Abteilungsziele:"
            "- Die Schadequote liegt mit 32,05% derzeit im Zielbereich (Zielgröße 50,00 %)."
            "Teamziele:"
            "- Im Team wurde der Zielwert des Bestands i.H.v. 142.000 € noch nicht erreicht. Aktuell liegt der Bestand bei 69.015€."
            "- Der Zielwert des Neu-/Mehrgeschäftes i.H.v. 164.798 € wurde bislang noch nicht erreicht und beträgt derzeit 75.256 €."
            "Persönliche Ziele:"
            "Bestandsziele:"
            "- 2 von 5 Maklern konnten den Bestand (Privat + SMC) im Vergleich zum Vorjahr steigern." 
            "- 3 von 8 Maklern konnten den Bestand (Firmen MC) im Vergleich zum Vorjahr steigern."
            "Ingesamt hat Dein Maklerportfolio ein Bestandsvolument von X TEUR, im VJ wurden X TEUR erreicht."
            "Neu-/Mehrgeschäftsziele:"
            "- 4 von 8 Makern konnten das Neu/Mehrgeschäft(Privat + SMC) im Vergleich zum Vorjahr steigern. "
            "- 4 von 8 Makern konnten das Neu/Mehrgeschäft(Firmen MC) im Vergleich zum Vorjahr steigern. "
            "Ingesamt hat Dein Maklerportfolio ein Neu-/Mehrgeschäft von X TEUR, im VJ wurden X TEUR erreicht."
            "Produktive Makler:"
            "- 4 von 7 Maklern sind bereits produktiv."
            "Wenn Du möchtest gebe ich Dir gerne eine Detailssicht zu Deinem Maklerportfolio und empfehle Maßnahmen, um Deine persönlichen Ziele effizient zu erreichen. "
        }
    
    temp_thread = create_thread("Ermittle die grundsätzliche Definition für folgende Zielarten entsprechend deiner Knowledge Base: 1. Abteilungsziele, 2. Teamziele, 3. Persönliche Ziele inkl. Bestandsziele, Neu- Mehrgeschäftsziele und Produktive Makler")
    logger.info(f'Temp thread created with ID: {temp_thread.id}')
    temp_run1 = client.beta.threads.runs.create_and_poll(thread_id=temp_thread.id, assistant_id=assistant.id)
    
    if temp_run1.status == 'completed':
        logger.info('target_analyze run step 1 completed successfully')
        temp_thread_message = client.beta.threads.messages.create(
                    temp_thread.id,
                    role="user",
                    content=(
                        f'Wende diese Definitionen auf die folgenden Maklervertrieb Zahlen an und erstelle eine übergreifende Auflistung der Zielarten mit ihrem aktuellen Erreichungsgrad (einschließlich kurzer Zusammenfassung der Zielart-Definition):'
                        f'Hier sind die Maklervertrieb Zahlen: {result_json}'
                        f'Strukturiere deine Antwort entsprechend folgendem Beispiel: {output_template}'
                    ),
                )
        
        temp_run2 = client.beta.threads.runs.create_and_poll(thread_id=temp_thread.id, assistant_id=assistant.id)
        if temp_run2.status == 'completed':
            temp_messages = list(client.beta.threads.messages.list(thread_id=temp_thread.id))
            response = temp_messages[0]
            logger.info('target_analyze run step 2 completed successfully')
            return response
        logger.info(f'Problem: target_analyze run step 2 not completed: {temp_run2.status}')
        
    logger.info(f'Problem: target_analyze run step 1 not completed: {temp_run1.status}')
    return jsonify({"error": "An error occurred during the analysis"}), 500

def team_analyze():
    return 'Max Mustermann hat eine Performance = 65%, Dieter Hans hat eine Performance = 82%, Ulrich Mark hat eine Performance = 85% '

def create_appointment_task():
    logger.info('create_appointment_task function triggered')
    return '05.10. 14:15 ; 07.10 16:35'
    
def create_appointment():
    return 'Termin wurde im Kalender hinterlegt.'
    
def productive_broker_analyze(path):
    logger.info('productive_broker_analyze')
    data = pd.read_excel(path, engine='openpyxl')
    result_json = data.to_json(orient='records', indent=4)
    
    temp_thread = create_thread("Ermittle die Definition für produktive Makler entsprechend deiner Knowledge Base.")
    logger.info(f'Temp thread created with ID: {temp_thread.id}')
    temp_run1 = client.beta.threads.runs.create_and_poll(thread_id=temp_thread.id, assistant_id=assistant.id)
    
    if temp_run1.status == 'completed':
        logger.info('productive_broker_analyze run step 1 completed successfully')
        temp_thread_message = client.beta.threads.messages.create(
                    temp_thread.id,
                    role="user",
                    content=(
                        f'Wende diese Definition auf die folgenden Maklervertrieb Zahlen an und sage mir welche Makler entsprechend dieser Definition produktiv sind: {result_json}'
                    ),
                )
        
        temp_run2 = client.beta.threads.runs.create_and_poll(thread_id=temp_thread.id, assistant_id=assistant.id)
        if temp_run2.status == 'completed':
            logger.info('productive_broker_analyze run step 2 completed successfully')
            temp_messages = list(client.beta.threads.messages.list(thread_id=temp_thread.id))
            response = temp_messages[0]
            return response
        logger.info(f'Problem: productive_broker_analyze run step 2 not completed: {temp_run1.status}')
        
    logger.info(f'Problem: productive_broker_analyze run step 1 not completed: {temp_run1.status}')
    return jsonify({"error": "An error occurred during the analysis"}), 500

def create_output(run, tool_calls, path, thread):
    tool_outputs = []
    for tool in tool_calls:
        if tool.function.name == "team_analyze":
            result = team_analyze()
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Aktuelle Team Performancedaten: {result}'
            })
        elif tool.function.name == "create_appointment":
            result = team_analyze()
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Kalendernachricht: {result}'
            })
        elif tool.function.name == "create_appointment_task":
            result = create_appointment_task()
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Es gibt Mögliche freie Termine am : {result}'
            })
        elif tool.function.name == "target_analyze":
            result = target_analyze(path)
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Im Folgenden findest Du eine aktuelle Übersicht über die quantitative Zielerreichung: {result}'
            })
        elif tool.function.name == "productive_broker_analyze":
            result = productive_broker_analyze(path)
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'{result}'
            })

    if tool_outputs:
        try:
            run = client.beta.threads.runs.submit_tool_outputs_and_poll(
                thread_id=thread.id,
                run_id=run.id,
                tool_outputs=tool_outputs
            )
            logger.info('Tool outputs submitted successfully.')
        except Exception as e:
            logger.info(f'Failed to submit tool outputs: {e}')
    else:
        logger.info('No tool outputs to submit.')

def create_thread(content_user_input):
    thread = client.beta.threads.create()
    message = client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=content_user_input,
    )
    return thread

def extract_and_format_content(message_content):
    """Extract, convert, and format the content of the message."""
    try:
        if isinstance(message_content, str):
            content = message_content
        elif hasattr(message_content, 'value'):
            content = message_content.value
        elif hasattr(message_content, 'text'):
            if hasattr(message_content.text, 'value'):
                content = message_content.text.value
            else:
                content = message_content.text
        else:
            content = str(message_content)
        
        formatted_content = format_message_content(content)
        return formatted_content
    except Exception as e:
        logger.info(f'Error extracting and formatting content: {e}')
        return ""

def generate_follow_up_questions(response_text):
    if not isinstance(response_text, str):
        response_text = str(response_text)

    response_lower = response_text.lower()
    questions = []
    
    if 'quantitative zielerreichung' in response_lower:
        questions.append("Wie erreiche ich meine persönlichen Ziele?")
        questions.append("Wie erreichen wir unsere Teamziele?")
        questions.append("Welcher Vertriebsschwerpuntk könnte mir dabei helfen, meine persönlichen Ziele zu erreichen?")
    if 'persönlichen ziele' in response_lower:
        questions.append("Wird einer der Top Accounts zukünftig produktiv?")
        questions.append("Haben andere KollegInnen im MV ähnliche Vertriebsschwerpunkte und Geschäftsverteilungen?")
    if not questions:
        questions.append("Erzähle mir mehr.")
    
    return questions

def process_message(message):
    response = []
    if hasattr(message, 'content'):
        if isinstance(message.content, list):
            for content_item in message.content:
                if hasattr(content_item, 'text') or hasattr(content_item, 'value'):
                    formatted_content = extract_and_format_content(content_item)
                    response.append({"role": "assistant", "content": formatted_content})
                    logger.info(f'Processed content 1: {formatted_content}')
                    break  # Only handle the first relevant content
    return response

@app.route('/', methods=['GET', 'POST'])
def home():
    if request.method == 'POST' and 'document' in request.files:
        file = request.files['document']
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            return redirect(url_for('home'))
    uploaded_files = os.listdir(app.config['UPLOAD_FOLDER'])
    return render_template('index.html', uploaded_files=uploaded_files)
    
@app.route('/check_status', methods=['GET'])
def check_status():
    logger.info('check_status called')
    if task_completed.is_set():
        logger.info('Task completed')
        if 'error' in analysis_result:
            return jsonify({"status": "error", "error": analysis_result['error']}), 500
        return jsonify({"status": "completed", "response": analysis_result.get('response'), "messages": analysis_result.get('messages', []), "suggestions": analysis_result.get('suggestions', [])})
    logger.info('Task still running')
    return jsonify({"status": "running"})

@app.route('/chat', methods=['POST'])
def chat():
    global thread
    global analysis_result

    content_user_input = request.json.get('user_input')
    logger.info(f"Received user input: {content_user_input}")
    
    if thread is None:
        thread = create_thread(content_user_input)
        logger.info(f'User thread created with ID: {thread.id}')
    else: #only create message in thread if there is already a thread
        thread_message = client.beta.threads.messages.create(
            thread.id,
            role="user",
            content=content_user_input,
        )
    
    path = os.path.join(base_dir, 'uploads', 'docs', 'maklervertrieb_zahlen_v0.3.xlsx')
    
    task_completed.clear()  # Reset task event
    analysis_result = {"status": "running"}

    def analyze_task():
        global analysis_result
        try:
            logger.info("Starting long running task")
            run = client.beta.threads.runs.create_and_poll(thread_id=thread.id, assistant_id=assistant.id)
            logger.info(f'Run created: {run.id}')
            
            if run.status in ['completed', 'requires_action']:
                if run.status == 'requires_action':
                    tool_calls = run.required_action.submit_tool_outputs.tool_calls
                    create_output(run, tool_calls, path, thread)
                    logger.info("Tool outputs created")
    
                messages = list(client.beta.threads.messages.list(thread_id=thread.id))
                logger.info(f'Messages retrieved: {len(messages)}')
                logger.info(f'Messages: {messages}')
                
                response = process_message(messages[0])
                last_message = messages[-1].content if messages else ""
    
                if isinstance(last_message, list):
                    last_message_text = " ".join(extract_and_format_content(item) for item in last_message)
                else:
                    last_message_text = extract_and_format_content(last_message)
                
                suggestions = generate_follow_up_questions(last_message_text)
                analysis_result['messages'] = response
                analysis_result['suggestions'] = suggestions
                logger.info("Task completed successfully")
            
            task_completed.set()
        except Exception as e:
            analysis_result['error'] = str(e)
            logger.error("An error occurred during the analysis task", exc_info=True)
            
        finally:
            task_completed.set()  # Setze das Event, unabhängig vom Ergebnis der Aufgabe
    
    logger.info("Starting background task")
    threading.Thread(target=analyze_task).start()
    #return jsonify({"status": "running"})
    '''
    task_success = task_completed.wait(timeout=300)

    if not task_success:
        return "Task is still running. Please check back later.", 202

    if 'error' in analysis_result:
        return f"An error occurred: {analysis_result['error']}", 500
    '''
    return jsonify(analysis_result)
    

if __name__ == '__main__':
    logger.info('Main executed')
    initialize_resources()
    #app.run(host='0.0.0.0', port=8080, debug=False)
    app.run(host='0.0.0.0', port=8080, threaded=True, use_reloader=False)