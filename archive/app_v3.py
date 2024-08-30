from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
import os
import openai
import pandas as pd
import re
import json
import boto3
from botocore.exceptions import ClientError

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
            "name": "advise_for_personal_target",
            "description": "Provide advice on how to reach personal goals based on the account manager's current performance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "AccountManagerID": {
                        "type": "string",
                        "description": "The unique identifier of the account manager, e.g., AM12345"
                    },
                    "personalTargetID": {
                        "type": "string",
                        "description": "The unique identifier of the personal target, e.g., PT12345"
                    }
                },
                "required": ["AccountManagerID", "personalTargetID"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "advise_for_team_target",
            "description": "Provide advice on how to reach team goals based on current performance."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "advise_for_new_business_target",
            "description": "Provide advice on how to improve new business targets based on potential analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "AccountManagerID": {
                        "type": "string",
                        "description": "The unique identifier of the account manager, e.g., AM12345"
                    }
                },
                "required": ["AccountManagerID"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "advise_for_productive_brokers",
            "description": "Provide advice on how to make brokers productive based on comparable brokers' data.",
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
        print(f"Assistant created with ID: {assistant.id}")
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
    print(file_batch.status)
    print(file_batch.file_counts)

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
    print("target_analyze function triggered")
    df = pd.read_excel(file_path, engine='openpyxl')

    overview = {
        "produktive_makler": [],
        "neumehrbeitrag": [],
        "bestandsbeitrag": [],
        "teamziele": []
    }

    for ix, row in df.iterrows():
        account_manager = row['(Benutzerschlüssel), Account Manager']
        account_name = row['Account Name']
        abteilung = row['MSN06, Strukturnummer']
        team = row['MSN12, Strukturnummer']
        branche = row['Branche ']
        sollen = row['Soll']
        ist_bestand = row['Ist, Bestand gesamt']
        vorjahr_bestand = row['Vorjahr, Bestand gesamt']
        ist_neu_mehr = row['Ist, Neu-/Mehrgeschäft']
        ist_angebote = row['Ist, Gerechnete Angebote']
        status = row['Ist, Status der Angebote']

        # Übersicht deiner Produktiven Makler
        overview["produktive_makler"].append({
            "account_name": account_name,
            "ist_bestand_ist": ist_bestand,
            "vorjahr_bestand": vorjahr_bestand,
            "neu-mehr-geschäft": ist_neu_mehr,
        })

        # Übersicht Neumehrbeitrag
        neumehrbeitrag_prozent = (ist_neu_mehr / sollen) * 100 if sollen else 0
        overview["neumehrbeitrag"].append({
            "account_name": account_name,
            "team": team,
            "neumehrbeitrag": ist_neu_mehr,
            "prozentualer_wert_an_ziele": f"{neumehrbeitrag_prozent:.2f}%"
        })

        # Übersicht Bestandsbeitrag
        bestandsbeitrag_prozent = (ist_bestand / sollen) * 100 if sollen else 0
        overview["bestandsbeitrag"].append({
            "account_name": account_name,
            "team": team,
            "bestandsbeitrag": ist_bestand,
            "prozentualer_wert_an_ziele": f"{bestandsbeitrag_prozent:.2f}%"
        })
        
        # Übersicht Teamziele
        overview["teamziele"].append({
            "team": team,
            "neugeschäft": ist_neu_mehr,
            "bestand": ist_bestand
        })
    return overview

def team_analyze():
    return 'Max Mustermann hat eine Performance = 65%, Dieter Hans hat eine Performance = 82%, Ulrich Mark hat eine Performance = 85% '

def create_appointment_task():
    print("create_appointment_task function triggered")
    return '05.10. 14:15 ; 07.10 16:35'
    
def create_appointment():
    '''
    thread_message = client.beta.threads.messages.create(
                thread.id,
                role="user",
                content="How does AI work? Explain it in simple terms.",
            )
            
    messages = list(client.beta.threads.messages.list(thread_id=thread.id))
    
    run = client.beta.threads.runs.create_and_poll(thread_id=thread.id, assistant_id=assistant.id)
    response = process_message(messages[0])
    return response
    '''
    return 'Termin wurde im Kalender hinterlegt.'
    
def productive_broker_analyze(path):
    data = pd.read_excel(path, engine='openpyxl')
    

    def clean_currency(value):
        try:
            return float(value.replace('€', '').replace(',', '').replace('%', '').strip())
        except:
            return 0.0  

    def is_productive(row):
        bestandswert_vorjahr = clean_currency(row['Vorjahr, Bestand gesamt'])
        zielwert = clean_currency(row['Soll'])
        neugeschaeft_ist = clean_currency(row['Ist, Neu-/Mehrgeschäft'])

        condition1 = neugeschaeft_ist >= zielwert
        
        condition2 = neugeschaeft_ist >= 0.20 * bestandswert_vorjahr
        
        condition3 = neugeschaeft_ist >= 25.000
        
        return condition1 and condition2

    data['Produktiv'] = data.apply(is_productive, axis=1)

    return data

def productive_broker_analyze_temp(path):

        data = pd.read_excel(path, engine='openpyxl')
        

        result_json = data.to_json(orient='records', indent=4)
        
        return result_json

def advise_for_personal_target():
    return None
    
def advise_for_team_target():
    return None
    
def advise_for_new_business_target():
    return None
    
def advise_for_productive_brokers():
    return None

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
            result = json.dumps(result, indent=4)
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Im Folgenden findest Du eine aktuelle Übersicht über die quantitative Zielerreichung: {result}'
            })
        elif tool.function.name == "productive_broker_analyze":
            print('productive_broker_analyze')
            result_json = productive_broker_analyze_temp(path)
            #result_json = result.to_json(orient='records', indent=4)
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f' produktive Makler (individuell entsprechend des Maklerportfolios festgesetzt); grds. produktiv, wenn Bestand > Vorjahr und Neu-/Mehrgeschäft i.H.v. 20% des Bestandes (min. aber 25.000€). Hier hast du eine Liste mit den aktuellen zahlen, bitte analysiere sie noch nach der definition der produktiven makler. : {result_json}'
                #"output": f'Ermittle die Kriterien für Produktiver Makler aus den Beschreibungen in Dokument Zieldefinition MV v1.pdf in deiner Knowledge Base. Gib eine kurze Definition von Produktiver Makler. Wende die Definition von produktiver Makler auf folgende Daten von Maklern an und gib eine Liste der produktiven Makler zurück. : {result_json}'
            })
        elif tool.function.name == "advise_for_personal_target":
            result = advise_for_personal_target()
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Vorschläge um deine persönlichen Ziele zu erreichen: {result}'
            })
        elif tool.function.name == "advise_for_team_target":
            result = advise_for_team_target()
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Vorschläge um eure Teamziele zu erreichen: {result}'
            })
        elif tool.function.name == "advise_for_new_business_target":
            result = advise_for_new_business_target()
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Vorschläge zur Verbesserung der Neumehrziele: {result}'
            })
        elif tool.function.name == "advise_for_productive_brokers":
            result = advise_for_productive_brokers()
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Potenzial bei vergleichbaren Maklern: {result}'
            })

    if tool_outputs:
        try:
            run = client.beta.threads.runs.submit_tool_outputs_and_poll(
                thread_id=thread.id,
                run_id=run.id,
                tool_outputs=tool_outputs
            )
            print("Tool outputs submitted successfully.")
        except Exception as e:
            print("Failed to submit tool outputs:", e)
    else:
        print("No tool outputs to submit.")

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
        print(f"Error extracting and formatting content: {e}")
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
                    print(f"Processed content 1: {formatted_content}")
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

@app.route('/chat', methods=['POST'])
def chat():
    global thread

    try:
        content_user_input = request.json.get('user_input')
        print("Received user input:", content_user_input)
        
        if thread is None:
            thread = create_thread(content_user_input)
            print(f"Thread created with ID: {thread.id}")
        else: #only create message in thread if there is already a thread
            thread_message = client.beta.threads.messages.create(
                thread.id,
                role="user",
                content=content_user_input,
            )
        
        path = os.path.join(base_dir, 'uploads', 'docs', 'maklervertrieb_zahlen_v0.3.xlsx')
        
        run = client.beta.threads.runs.create_and_poll(thread_id=thread.id, assistant_id=assistant.id)
        print("Run created:", run.id)
        
        if run.status in ['completed', 'requires_action']:
            if run.status == 'requires_action':
                tool_calls = run.required_action.submit_tool_outputs.tool_calls
                create_output(run, tool_calls, path, thread)
                print("Tool outputs created")

            messages = list(client.beta.threads.messages.list(thread_id=thread.id))
            print("Messages retrieved:", len(messages))
            print("Messages:", messages)

            response = process_message(messages[0])
            last_message = messages[-1].content if messages else ""

            if isinstance(last_message, list):
                last_message_text = " ".join(extract_and_format_content(item) for item in last_message)
            else:
                last_message_text = extract_and_format_content(last_message)
            
            suggestions = generate_follow_up_questions(last_message_text)
            
            return jsonify({"messages": response, "suggestions": suggestions})
    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("Main executed")
    initialize_resources()
    app.run(host='0.0.0.0', port=8080, debug=False)