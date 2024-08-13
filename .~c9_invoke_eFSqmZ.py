from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
import os
import openai
import pandas as pd
import re
from dotenv import load_dotenv

# Load the environment variables from .env file if it exists
load_dotenv()

# Initialize OpenAI client
api_key = os.getenv('OPENAI_API_KEY')
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
    # Bold text
    formatted_content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', content)
    # Headers
    formatted_content = re.sub(r'###### (.*?)\n?', r'<h6>\1</h6>', formatted_content)
    formatted_content = re.sub(r'##### (.*?)\n?', r'<h5>\1</h5>', formatted_content)
    formatted_content = re.sub(r'#### (.*?)\n?', r'<h4>\1</h4>', formatted_content)
    formatted_content = re.sub(r'### (.*?)\n?', r'<h3>\1</h3>', formatted_content)
    formatted_content = re.sub(r'## (.*?)\n?', r'<h2>\1</h2>', formatted_content)
    formatted_content = re.sub(r'# (.*?)\n?', r'<h1>\1</h1>', formatted_content)
    # Convert new lines to <br>
    formatted_content = formatted_content.replace('\n', '<br>')
    return formatted_content

# OpenAI Assistant Configuration
function_calling_tool = [
    {
        "type": "function",
        "function": {
            "name": "soll_ist_analyze",
            "description": "Get the performance of the broker in terms of current achievement compared to the target.",
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
            "name": "create_jira_task",
            "description": "Create a Jira Task for a one-to-one dialogue with an employee about their performance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "brokerID": {
                        "type": "string",
                        "description": "The unique identifier of the broker, e.g., BR12345"
                    },
                    "Datum": {
                        "type": "string",
                        "description": "The date for scheduling the dialogue, e.g., YYYY-MM-DD"
                    }
                },
                "required": ["brokerID", "Datum"]
            }
        }
    }
]

file_search_tool = {
    "type": "file_search"
}

def create_assistant(client, function_calling_tool, file_search_tool):
    assistant = client.beta.assistants.create(
        name="Broker Assistant",
        instructions=(
            "You are an expert performance advisor to insurance brokers. "
            "Use your knowledge base to answer performance questions and give them tips "
            "on how to improve their potential and reach their targets."
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

def team_analyze():
    return 'Max Mustermann hat eine Performance = 65%, Dieter Hans hat eine Performance = 82%, Ulrich Mark hat eine Performance = 85% '

def create_jira_task():
    return 'Der Task wurde mit der ID LT45 in Jira hinterlegt.'

def create_output(run, tool_calls, path, thread):
    tool_outputs = []
    for tool in tool_calls:
        if tool.function.name == "soll_ist_analyze":
            result = soll_ist_analyze('815', path)
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Aktuelle Performancedaten: {result}'
            })
        elif tool.function.name == "team_analyze":
            result = team_analyze()
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Aktuelle Team Performancedaten: {result}'
            })
        elif tool.function.name == "create_jira_task":
            result = create_jira_task()
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Jira Task Ergebnis: {result}'
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
    
    if 'performance' in response_lower:
        questions.append("How can I improve my performance?")
        questions.append("What are the key metrics?")
    if 'team' in response_lower:
        questions.append("How is the team performing?")
        questions.append("What are the individual team member stats?")
    if not questions:
        questions.append("Can you tell me more?")
    
    return questions

def process_messages(messages, user_input):
    conversation_history = []
    for message in messages:
        if hasattr(message, 'content'):
            if isinstance(message.content, list):
                for content_item in message.content:
                    if hasattr(content_item, 'text') or hasattr(content_item, 'value'):
                        formatted_content = extract_and_format_content(content_item)
                        # Skip messages that just repeat the user input
                        if formatted_content.lower() != user_input.lower():
                            conversation_history.append({"role": "assistant", "content": formatted_content})
                            print(f"Processed content: {formatted_content}")
                            break  # Only handle the first relevant content
            else:
                formatted_content = extract_and_format_content(message.content)
                # Skip messages that just repeat the user input
                if formatted_content.lower() != user_input.lower():
                    conversation_history.append({"role": "assistant", "content": formatted_content})
                    print(f"Processed content: {formatted_content}")
    return conversation_history

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
    try:
        print("Creating assistant...")
        assistant = create_assistant(client, function_calling_tool, file_search_tool)
        
        content_user_input = request.json.get('user_input')
        print("Received user input:", content_user_input)
        
        file_paths_bucket = [os.path.join(base_dir, 'uploads', 'docs', filename) for filename in ['Input_1_sales.pdf', 'Input_2_Mitarbeitergespräche.pdf', 'Input_3_Leistungsabfall_roadmap.pdf']]
        create_data_base(file_paths_bucket, assistant.id)
        
        path = os.path.join(base_dir, 'uploads', 'docs', 'Maklervertrieb_Zahlen_v0.2_wip.xlsx')
        
        thread = create_thread(content_user_input)
        print("Thread created:", thread.id)
        
        run = client.beta.threads.runs.create_and_poll(thread_id=thread.id, assistant_id=assistant.id)
        print("Run created:", run.id)
        
        if run.status in ['completed', 'requires_action']:
            if run.status == 'requires_action':
                tool_calls = run.required_action.submit_tool_outputs.tool_calls
                create_output(run, tool_calls, path, thread)
                print("Tool outputs created")

            messages = list(client.beta.threads.messages.list(thread_id=thread.id))
            print("Messages retrieved:", len(messages))

            conversation_history = process_messages(messages, content_user_input)
            last_message = messages[-1].content if messages else ""

            if isinstance(last_message, list):
                last_message_text = " ".join(extract_and_format_content(item) for item in last_message)
            else:
                last_message_text = extract_and_format_content(last_message)
            
            print(f"Processed last message: {last_message_text}")
            
            suggestions = generate_follow_up_questions(last_message_text)
            
            return jsonify({"messages": conversation_history, "suggestions": suggestions})
    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)