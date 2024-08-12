from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
import os
import openai
import pandas as pd
from dotenv import load_dotenv

# Load the environment variables from .env file if it exists
load_dotenv()

# Initialize OpenAI client
api_key = os.getenv('OPENAI_API_KEY')
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

# Helper functions for file uploads
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# OpenAI Assistant Configuration
function_calling_tool = {
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
}

file_search_tool = {
    "type": "file_search"
}

assistant = client.beta.assistants.create(
    name="Broker Assistant",
    instructions=(
        "You are an expert performance advisor to insurance brokers. "
        "Use your knowledge base to answer performance questions and give them tips on how to improve their potential and reach their targets."
    ),
    model="gpt-4o-mini",
    tools=[file_search_tool, function_calling_tool]
)

# Initialize a global variable to hold the thread ID
thread_id = None

# Route for home page and uploading documents
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

# Route to handle chat functionality with AJAX
@app.route('/chat', methods=['POST'])
def chat():
    global thread_id
    user_input = request.json.get('user_input')
    conversation_history = []

    # Create a new thread if it doesn't exist
    if thread_id is None:
        thread = client.beta.threads.create()
        thread_id = thread.id
    else:
        thread = client.beta.threads.retrieve(thread_id)

    # Add user message to the thread
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=user_input,
    )

    # Run the assistant
    run = client.beta.threads.runs.create_and_poll(
        thread_id=thread.id,
        assistant_id=assistant.id
    )

    # Process the run result
    if run.status == 'completed':
        messages = list(client.beta.threads.messages.list(thread_id=thread.id, run_id=run.id))
        for message in messages:
            message_content = get_message_content(message)
            conversation_history.append({"role": "assistant", "content": message_content})
    elif run.status == 'required_action':
        tool_calls = run.required_action.submit_tool_outputs.tool_calls
        create_output(run, tool_calls, os.path.join(app.config['UPLOAD_FOLDER'], 'Maklervertrieb_Zahlen_v0.2_wip.xlsx'), thread)
        messages = list(client.beta.threads.messages.list(thread_id=thread.id))
        for message in messages:
            message_content = get_message_content(message)
            conversation_history.append({"role": "assistant", "content": message_content})

    return jsonify(conversation_history)

# Function to retrieve and convert message content to JSON-serializable format
def get_message_content(message):
    try:
        # Navigate through the structure to get the value of the text content
        if isinstance(message.content, list):
            for content_block in message.content:
                if content_block.type == 'text':
                    return content_block.text.value
    except AttributeError:
        return str(message.content)
    return str(message.content)

# Function to perform performance analysis for brokers
def soll_ist_analyze(broker_number, file_path):
    df = pd.read_excel(file_path, engine='openpyxl')

    # Filter data by Broker Number
    broker_data = df.loc[df['BrokerID'] == int(broker_number)]

    if broker_data.empty:
        return f"No data found for broker number: {broker_number}"

    # Group and aggregate the data by Division and Product
    grouped_data = broker_data.groupby(['Sparte', 'Produkt']).agg({
        'Target_1': 'sum',
        'Target_2': 'sum',
        'Target_3': 'sum',
        'KPI_1': 'sum',
        'KPI_2': 'sum',
        'KPI_3': 'sum'
    }).reset_index()

    performance_list = []

    # Format the results
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

# Function to create tool outputs for required actions
def create_output(run, tool_calls, path, thread):
    tool_outputs = []

    for tool in tool_calls:
        if tool.function.name == "soll_ist_analyze":
            result = soll_ist_analyze('815', path)
            tool_outputs.append({
                "tool_call_id": tool.id,
                "output": f'Current Performance Data: {result}'
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

if __name__ == '__main__':
    # Run the Flask app on the specified host and port
    app.run(host='0.0.0.0', port=8080, debug=True)