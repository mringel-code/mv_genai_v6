from flask import Flask, render_template, request, redirect, url_for, jsonify, session
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
boto3_session = boto3.session.Session()
boto3_client = boto3_session.client(
    service_name='secretsmanager',
    region_name=region_name
)

try:
    get_secret_value_response = boto3_client.get_secret_value(
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
app.secret_key = 'your_secret_key'  # Set a secret key for session management

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
            "description": "Give an overview to the account manager on the status of achieving his targets (Zielerreichung)."
            #"description": "Get the performance of the portfolio of brokers in terms of quantitative targets achieved on department level, team level and personal level of the account manager."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "target_gap",
            "description": "Make suggestions on how to reach the personal targets (persönliche Ziele) of the the account manager."
            #"description": "Make suggestions on how to reach the personal targets (persönliche Ziele) of the the account manager by analyzing which parts of the portfolio to optimize given the correlation of targets."
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
            "description": "Get an overview of the brokers who can currently be labeled as productive according to target definition."
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
temp_assistant = None
temp_thread = None
kb_files = ['Input_1_sales.pdf', 'Zieldefinition MV v2.pdf', 'Maklervertrieb Zahlen v0.4.docx']

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
        temperature=0.1,
        tools=[file_search_tool] + function_calling_tool
    )
    return assistant

def initialize_assistant_for_session():
    assistant = create_assistant(client, function_calling_tool, file_search_tool)
    session['assistant_id'] = assistant.id
    logger.info(f"Assistant created with ID: {assistant.id}")
    file_paths_bucket = [os.path.join(base_dir, 'uploads', 'docs', filename) for filename in kb_files]
    create_data_base(file_paths_bucket, assistant.id)
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
    
def run_prompts_with_temp_thread(function, prompt_steps):
    global temp_thread
    global temp_assistant
    
    if temp_thread is None:
        temp_thread = client.beta.threads.create()
        logger.info(f'Temp thread created with ID: {temp_thread.id}')
    
        temp_assistant = client.beta.assistants.create(
            name="Broker Assistant",
            instructions=(
                "You are an expert performance advisor helping an account manager manage the performance of his insurance broker accounts. "
                "Use your knowledge base to answer questions and refer to the sources from your knowledge base you used to answer the question in your response. "
                "Give your answers in german."
            ),
            model="gpt-4o-mini",
            temperature=0.1,
            tools=[file_search_tool]
        )
        logger.info(f"Temp assistant created with ID: {assistant.id}")
        file_paths_bucket = [os.path.join(base_dir, 'uploads', 'docs', filename) for filename in kb_files]
        create_data_base(file_paths_bucket, temp_assistant.id)
    
    for i, step in enumerate(prompt_steps):
        logger.info(f"Running prompt step {i+1} of {function}")
        
        temp_thread_message = client.beta.threads.messages.create(
            temp_thread.id,
            role="user",
            content=step
        )
        
        temp_run = client.beta.threads.runs.create_and_poll(thread_id=temp_thread.id, assistant_id=temp_assistant.id)
        logger.info(list(client.beta.threads.messages.list(thread_id=temp_thread.id)))
        if temp_run.status != 'completed':
            logger.info(f'Problem: {function} run step {i+1} not completed: {temp_run.status}')
            with app.app_context():
                return jsonify({"error": "An error occurred during the analysis"}), 500
    
    temp_messages = list(client.beta.threads.messages.list(thread_id=temp_thread.id))
    response = temp_messages[0]
    logger.info(f'{function} completed successfully')
    
    with app.app_context():
        return response

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
        
    output_template_final = {
            "Abteilungsziele:"
            "\n- Die Schadequote liegt mit 32,05% derzeit im Zielbereich (Zielgröße 50,00 %)."
            "\n\nTeamziele:"
            "\n- Im Team wurde der Zielwert des Bestands i.H.v. 142.000 € noch nicht erreicht. Aktuell liegt der Bestand bei 69.015€.\n"
            "\n- Der Zielwert des Neu-/Mehrgeschäftes i.H.v. 164.798 € wurde bislang noch nicht erreicht und beträgt derzeit 75.256 €."
            "\nPersönliches Ziel:"
            "\nBestandsziele:"
            "\n- 2 von 5 Maklern konnten den Bestand (Privat + SMC) im Vergleich zum Vorjahr steigern.\n" 
            "\n- 3 von 8 Maklern konnten den Bestand (Firmen MC) im Vergleich zum Vorjahr steigern.\n"
            "\nIngesamt hat Dein Maklerportfolio ein Bestandsvolument von X TEUR, im VJ wurden X TEUR erreicht.\n\n"
            "\nPersönliches Ziel:"
            "\nNeu-/Mehrgeschäftsziele:"
            "\n- 4 von 8 Makern konnten das Neu/Mehrgeschäft(Privat + SMC) im Vergleich zum Vorjahr steigern. "
            "\n- 4 von 8 Makern konnten das Neu/Mehrgeschäft(Firmen MC) im Vergleich zum Vorjahr steigern. "
            "\nIngesamt hat Dein Maklerportfolio ein Neu-/Mehrgeschäft von X TEUR, im VJ wurden X TEUR erreicht."
            "\nPersönliches Ziel:"
            "\nProduktive Makler:"
            "\n- 4 von 7 Maklern sind bereits produktiv."
            "\n\nWenn Du möchtest gebe ich Dir gerne eine Detailssicht zu Deinem Maklerportfolio und empfehle Maßnahmen, um Deine persönlichen Ziele effizient zu erreichen. "
        }
    
    result1 = get_abteilungsziele()
    result2 = get_teamziele()
    result3 = get_bestandsziele()
    result4 = get_neugeschaeftsziele()
    result5 = get_produktive_makler()
    
    prompt_steps = [
            f'Hier sind einzelne Teilergebnisse: \nAbteilungsziele: {result1} \nTeamziele: {result2} \nBestandsziele: {result3} \nNeu-/Mehrgeschäftsziele: {result4} \nnProduktive Makler: {result5} \n\nFasse die Ergebnisse entsprechend folgendem Beispiel zusammen: {output_template_final}'
        ]
    
    with app.app_context():
        return run_prompts_with_temp_thread("target_analyze", prompt_steps)

def get_abteilungsziele():
    prompt_steps = [
            f'Ermittle die Definition für die Zielart 1 Abteilungsziele und wende diese Definition auf die vorliegenden Maklervertrieb Zahlen. Erstelle daraus eine Auflistung der Kennzahlen mit ihrem aktuellen Erreichungsgrad! Antworte möglichst detailliert, da deine Antwort in anderen Abfragen als Input weiterverwendet werden soll.'
        ]
    
    with app.app_context():
        return run_prompts_with_temp_thread("get_abteilungsziele", prompt_steps)
        
def get_teamziele():
    prompt_steps = [
            f'Ermittle die Definition für die Zielart 2 Teamziele und wende diese Definitionen auf die vorliegenden Maklervertrieb Zahlen an. Erstelle daraus eine Auflistung der Kennzahlen mit ihrem aktuellen Erreichungsgrad! Antworte möglichst detailliert, da deine Antwort in anderen Abfragen als Input weiterverwendet werden soll.'
        ]
    
    with app.app_context():
        return run_prompts_with_temp_thread("get_teamziele", prompt_steps)
        
def get_bestandsziele():
    prompt_steps = [
            f'Ermittle die Definition für die Messgröße Bestandsziele innerhalb der Zielart 3 Persönliche Ziele und wende diese Definitionen auf die vorliegenden Maklervertrieb Zahlen an. Erstelle daraus eine Auflistung der Makler, die diese Zielvorgaben erreichen! Antworte möglichst detailliert, da deine Antwort in anderen Abfragen als Input weiterverwendet werden soll.'
        ]
    
    with app.app_context():
        return run_prompts_with_temp_thread("get_bestandsziele", prompt_steps)

def get_neugeschaeftsziele():
    prompt_steps = [
            f'Ermittle die Definition für die Messgröße Neu- Mehrgeschäft innerhalb der Zielart 3 Persönliche Ziele und wende diese Definition auf die vorliegenden Maklervertrieb Zahlen an. Erstelle daraus eine Auflistung der Makler, die diese Zielvorgaben erreichen! Antworte möglichst detailliert, da deine Antwort in anderen Abfragen als Input weiterverwendet werden soll.'
        ]
    
    with app.app_context():
        return run_prompts_with_temp_thread("get_bestandsziele", prompt_steps)
        
def get_produktive_makler():
    prompt_steps = [
            f'Ermittle die Definition für die Messgröße Produktive Makler innerhalb der Zielart 3 Persönliche Ziele und wende diese Definition auf die vorliegenden Maklervertrieb Zahlen. Erstelle daraus eine Auflistung der Makler, die diese Zielvorgaben erreichen! Antworte möglichst detailliert, da deine Antwort in anderen Abfragen als Input weiterverwendet werden soll.'
        ]
    
    with app.app_context():
        return run_prompts_with_temp_thread("get_produktive_makler", prompt_steps)
        
def target_gap(file_path):
    logger.info('target_gap function triggered')
    return 'Max Mustermann hat eine Performance = 65%, Dieter Hans hat eine Performance = 82%, Ulrich Mark hat eine Performance = 85% '

def team_analyze():
    return 'Max Mustermann hat eine Performance = 65%, Dieter Hans hat eine Performance = 82%, Ulrich Mark hat eine Performance = 85% '

def create_appointment_task():
    logger.info('create_appointment_task function triggered')
    return '05.10. 14:15 ; 07.10 16:35'
    
def create_appointment():
    return 'Termin wurde im Kalender hinterlegt.'
    
def productive_broker_analyze(path):
    logger.info('productive_broker_analyze function triggered')
    
    output_template = {
            "Im Folgenden findest Du eine Auflistung deiner produktiven Makler:"
            "\n\nMakler A Strukturnummer 1:"
            "\n- Bestand gesamt Ist: 9.000€, Bestand Gesamt Vorjahr: 10.000€; Teilkriterium Bestand Ist > Bestand Vorjahr: nicht erfüllt"
            "\n- Neu-/Mehrgeschäft Ist: 1.000€ Teilkriterium Neu-/Mehrgeschäft i.H.v. 20%  des Bestandes (min. aber 25.000€): nicht erfüllt"
            "\n- Produktiv Ja/Nein: Nein"
            "\n\nMakler B Strukturnummer 2:"
            "\n- Bestand gesamt Ist: 100.000€, Bestand Gesamt Vorjahr: 90.000€; Teilkriterium Bestand Ist > Bestand Vorjahr: erfüllt"
            "\n- Neu-/Mehrgeschäft Ist: 50.000€ Teilkriterium Neu-/Mehrgeschäft i.H.v. 20%  des Bestandes (min. aber 25.000€): erfüllt"
            "\n- Produktiv Ja/Nein: Ja"
            "\n\nMakler C Strukturnummer 3:"
            "\n- Bestand gesamt Ist: 100.000€, Bestand Gesamt Vorjahr: 110.000€; Teilkriterium Bestand Ist > Bestand Vorjahr: nicht erfüllt"
            "\n- Neu-/Mehrgeschäft Ist: 50.000€ Teilkriterium Neu-/Mehrgeschäft i.H.v. 20%  des Bestandes (min. aber 25.000€): erfüllt"
            "\n- Produktiv Ja/Nein: Nein"
            "\n\nMakler D Strukturnummer 4:"
            "\n- Bestand gesamt Ist: 100.000€, Bestand Gesamt Vorjahr: 90.000€; Teilkriterium Bestand Ist > Bestand Vorjahr: erfüllt"
            "\n- Neu-/Mehrgeschäft Ist: 20.000€ Teilkriterium Neu-/Mehrgeschäft i.H.v. 20%  des Bestandes (min. aber 25.000€): nicht erfüllt"
            "\n- Produktiv Ja/Nein: Nein"
            "\n\nMakler E Strukturnummer 5:"
            "\n- Bestand gesamt Ist: 100.000€, Bestand Gesamt Vorjahr: 90.000€; Teilkriterium Bestand Ist > Bestand Vorjahr: erfüllt"
            "\n- Neu-/Mehrgeschäft Ist: 25.000€ Teilkriterium Neu-/Mehrgeschäft i.H.v. 20%  des Bestandes (min. aber 25.000€): erfüllt"
            "\n- Produktiv Ja/Nein: Ja"
        }
    
    result = get_produktive_makler()

    prompt_steps = [
            f'Hier ist eine Auflistung der produktiven Makler: {result} \nFasse die Ergebnisse entsprechend folgendem Beispiel zusammen: {output_template}'
        ]
    
    with app.app_context():
        return run_prompts_with_temp_thread("productive_broker_analyze", prompt_steps)
    

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
                "output": f'Im Folgenden findest Du eine aktuelle Auflistung: {result}'
            })
        elif tool.function.name == "target_gap":
            result = target_gap(path)
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Ich habe Dein Maklerportfolio analysiert und Zielkorrelationen berücksichtigt um deine persönlichen Ziele effizient zu erreichen.: {result}'
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
    if 'assistant_id' not in session:
        assistant = initialize_assistant_for_session()
    else:
        assistant = client.beta.assistants.retrieve(session['assistant_id'])
    
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
    
@app.route('/reset_session', methods=['GET'])
def reset_session():
    session.clear()
    return redirect(url_for('home'))

@app.route('/chat', methods=['POST'])
def chat():
    global thread
    global analysis_result

    content_user_input = request.json.get('user_input')
    logger.info(f"Received user input: {content_user_input}")
    
    if 'assistant_id' not in session:
        assistant = initialize_assistant_for_session()
    else:
        assistant = client.beta.assistants.retrieve(session['assistant_id'])
    
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
    #app.run(host='0.0.0.0', port=8080, debug=False)
    app.run(host='0.0.0.0', port=8080, threaded=True, use_reloader=False)