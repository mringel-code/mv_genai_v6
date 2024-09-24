from flask import Flask, render_template, request, redirect, url_for, jsonify, session, Response, stream_with_context, current_app
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
import time

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

assistant = None
temp_assistant = None
code_assistant = None
user_id = None
thread = None
temp_thread = None
code_thread = None

kb_files = ['Input_1_sales.pdf', 'Zieldefinition MV v2.pdf', 'Maklervertrieb Zahlen v0.4.docx']

def initialize_assistant_for_session():
    global assistant
    global temp_assistant
    global code_assistant
    
    global thread
    global temp_thread
    
    assistant = client.beta.assistants.retrieve("asst_7Hx0vFUQZDlJd1aSRm8HjtjR")
    temp_assistant = client.beta.assistants.retrieve("asst_trlWRLh1q6z7OWMv2NWJI8OZ")
    code_assistant = client.beta.assistants.retrieve("asst_vCYUUTgCj266XT4fN1daUqjE")
    
    return assistant
    
def run_prompts_with_temp_thread(function, prompt_steps, data_steps):
    with current_app.app_context():
        global user_id
        global temp_assistant
        global temp_thread
        global code_assistant
        global code_thread
        
        #temp_assistant = client.beta.assistants.retrieve("asst_trlWRLh1q6z7OWMv2NWJI8OZ")
        multiple = True
        
        for i, (step, data_step) in enumerate(zip(prompt_steps, data_steps)):
            if i==len(prompt_steps): 
                    multiple = None
            
            if code_thread is None: code_thread = client.beta.threads.create()
            thread_message = client.beta.threads.messages.create(
                thread_id=code_thread.id,
                role="user",
                content=data_step,
            )
            
            code_stream = client.beta.threads.runs.create(
                thread_id = code_thread.id,
                assistant_id=code_assistant.id,
                stream=True,
            )
            
            handle_streaming_response(code_stream, user_id, None, None, multiple)
            
            if data_step is not None: 
                data = data_response
            else:
                data = None
            
            logger.info(f'data used: {data}')
            
            if temp_thread is None: temp_thread = client.beta.threads.create()
            thread_message = client.beta.threads.messages.create(
                thread_id=temp_thread.id,
                role="user",
                content=step+data,
            )
            
            temp_stream = client.beta.threads.runs.create(
                thread_id = temp_thread.id,
                assistant_id=temp_assistant.id,
                stream=True,
            )
            
            handle_streaming_response(temp_stream, user_id, None, None, multiple)
            
def run_prompt_with_code_interpreter_thread(function, prompt):
    global code_assistant
    global code_thread
    #code_assistant = client.beta.assistants.retrieve("asst_vCYUUTgCj266XT4fN1daUqjE")
    
    if code_thread is None: code_thread = client.beta.threads.create()
    thread_message = client.beta.threads.messages.create(
        thread_id=code_thread.id,
        role="user",
        content=prompt,
    )
    
    code = client.beta.threads.runs.create_and_poll(
        thread_id = code_thread.id,
        assistant_id=code_assistant.id,
    )
    
    if code.status in ['completed']:
        messages = list(client.beta.threads.messages.list(thread_id=code_thread.id))
        response = process_message(messages[0])
        
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
    logger.info('target_analyze function triggered')
    global team
    global abteilung
    
    prompt_steps = [
        """
        Erstelle eine Übersicht entsprechend folgendem Musterbeispiel: 
        "Ergebnis Zielerreichung:
        - Die Schadenquote liegt mit xx,xx% derzeit im Zielbereich (Zielgröße yy,yy %)."
        Begrenze deine Ausgabe auf die angegebene Übersicht.
        Nutze dafür die folgenden Daten:
        """,
        """
        Erstelle eine Übersicht entsprechend folgendem Musterbeispiel: 
        "Ergebnis Zielerreichung:
        - Im Team wurde der Zielwert des Bestands i.H.v. y € noch nicht erreicht. Aktuell liegt der Bestand bei x €.
        - Der Zielwert des Neu-/Mehrgeschäftes i.H.v. y € wurde bislang noch nicht erreicht und beträgt derzeit y €."
        Begrenze deine Ausgabe auf die angegebene Übersicht.
        Nutze dafür die folgenden Daten:
        """,
        """
        Erstelle eine Übersicht entsprechend folgendem Musterbeispiel: 
        "Ergebnis Zielerreichung:
        - x von y Maklern konnten den Bestand (Privat + SMC) im Vergleich zum Vorjahr steigern.
        - x von y Maklern konnten den Bestand (Firmen MC) im Vergleich zum Vorjahr steigern. 
        - Ingesamt hat Dein Maklerportfolio ein Bestandsvolument von X TEUR, im VJ wurden X TEUR erreicht."
        Begrenze deine Ausgabe auf die angegebene Übersicht.
        Nutze dafür die folgenden Daten:
        """,
        """
        Erstelle eine Übersicht entsprechend folgendem Musterbeispiel: 
        "Ergebnis Zielerreichung:
        - x von y Makern konnten das Neu/Mehrgeschäft (Privat + SMC) im Vergleich zum Vorjahr steigern.
        - x von y Makern konnten das Neu/Mehrgeschäft (Firmen MC) im Vergleich zum Vorjahr steigern. 
        - Ingesamt hat Dein Maklerportfolio ein Neu-/Mehrgeschäft von X TEUR, im VJ wurden X TEUR erreicht."
        Begrenze deine Ausgabe auf die angegebene Übersicht.
        Nutze dafür die folgenden Daten:
        """,
        """
        Erstelle eine Übersicht entsprechend folgendem Musterbeispiel:
        "Ergebnis Zielerreichung:
        - x von y Maklern sind bereits produktiv."
        Begrenze deine Ausgabe auf die angegebene Übersicht.
        Nutze dazu folgende Daten:
        """
        ]
    
    data_steps = [
        f"""
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe solange Du nicht explizit aufgefordert wirst.
        Schritt 1: Gib aus: "Zielart 1 Abteilungsziele: \nDatengrundlage:" 
        Schritt 2: Lade die Daten aus dem Tabellenblatt 'Abteilungsziele'
        Schritt 3: Ermittle 'Zielgröße', 'Durchschnittliche Schadenquote', 'Abteilungsziel erreicht (Ja/Nein)' für {abteilung}. Zielgrößen und Schadenquoten werden in Prozent angegeben.
        Schritt 4: Gib deine Ergebnisse klar und präzise aus.
        """,
        f"""
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe solange Du nicht explizit aufgefordert wirst.
        Schritt 1: Gib aus: "Zielart 2 Teamziele: \nDatengrundlage:"
        Schritt 2: Lade die Daten aus dem Tabellenblatt 'Teamziele Bestand' und setze die Spaltennamen explizit auf ['Team', 'Ist Bestand Summe', 'Vorjahr Bestand Summe', 'Teamziel Bestand erreicht (Ja/Nein)']. 
        Schritt 3: Ermittle 'Vorjahr Bestand Summe', 'Ist Bestand Summe' und 'Teamziel Bestand erreicht (Ja/Nein)' für {team} aus. 
        Schritt 4: Lade die Daten aus dem Tabellenblatt 'Teamziele Neugeschäft' und setze die Spaltennamen explizit auf ['Team', 'Ist Neu-/Mehrgeschäft Summe', 'Vorjahr Neu-/Mehrgeschäft Summe', 'Teamziel Neugeschäft erreicht (Ja/Nein)'].
        Schritt 5: Ermittle 'Vorjahr Neu-/Mehrgeschäft Summe', 'Ist Neu-/Mehrgeschäft Summe' und 'Teamziel Neugeschäft erreicht (Ja/Nein)' für das Team von Max Mustermann aus. 
        Schritt 6: Gib deine Ergebnisse klar und präzise aus.
        """,
        """
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe solange Du nicht explizit aufgefordert wirst.
        Schritt 1: Gib aus: "Zielart 3 Persönliche Ziele - Messgröße Bestandsziele: \nDatengrundlage:"
        Schritt 2: Lade die Daten aus Tabelle 'Pers_Ziele_Bestand' in Tabellenblatt 'Pers Ziele Bestand' 
        Schritt 3: Filtere die Daten auf die Makler Accounts von Account Manager 'Max Mustermann'. Wieviele Makler sind das?
        Schritt 4: Ermittle, wieviele Makler mit 'Branchensegment' "Privat" oder "SMC" das persönliche Ziel Bestand erreichen.
        Schritt 5: Ermittle, wieviele Makler mit 'Branchensegment' "MidCorp" das persönliches Ziel Bestand erreichen.
        Schritt 6: Ermittle, wie hoch das Bestandsvolumen des Maklerportfolios aktuell is und wie hoch es im Vorjahr war.
        Schritt 7: Gib deine Ergebnisse klar und präzise aus.
        """,
        """
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe solange Du nicht explizit aufgefordert wirst.
        Schritt 1: Gib aus: "Zielart 3 Persönliche Ziele - Messgröße Neu-/Mehrgeschäftsziele: \nDatengrundlage:"
        Schritt 2: Lade die Daten aus Tabelle 'Pers_Ziele_Neugeschäft' in Tabellenblatt 'Pers Ziele Neugeschäft' 
        Schritt 3: Filtere die Daten auf die Makler Accounts von Account Manager 'Max Mustermann'. Wieviele Makler sind das? 
        Schritt 4: Ermittle, wieviele Makler mit 'Branchensegment' "Privat" oder "SMC" das persönliche Ziel Neu-/Mehrgeschäft erreichen. 
        Schritt 5: Ermittle, wieviele Makler mit 'Branchensegment' "MidCorp" das persönliches Ziel Neu-/Mehrgeschäft erreichen. 
        Schritt 6: Ermittle, wie hoch das Neu-/Mehrgeschäftsvolumen des Maklerportfolio aktuell is und wie hoch es im Vorjahr war. 
        Schritt 7: Gib deine Ergebnisse klar und präzise aus.
        """,
        """
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe solange Du nicht explizit aufgefordert wirst.
        Schritt 1: Gib aus: "Zielart 3 Persönliche Ziele - Produktive Makler: \nDatengrundlage:"
        Schritt 2: Lade die Daten aus Tabelle 'Pers_Ziele_Produktive_Makler' in Tabellenblatt 'Persönl Ziele Produktive Makler' 
        Schritt 3: Ermittle, wieviele Makler Accounts dem Account Manager 'Max Mustermann' zugeordnet sind. 
        Schritt 4: Ermittle, wieviele davon das persönliche Ziel produktive Makler erreichen.
        Schritt 5: Gib deine Ergebnisse klar und präzise aus.
        """
        ]
        
    data_steps_bkp = [
        """
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe solange Du nicht explizit aufgefordert wirst.
        Schritt 1: Gib aus: "Zielart 1 Abteilungsziele: \nDatengrundlage:" 
        Schritt 2: Lade die Daten aus dem Tabellenblatt 'Teamzuordnung' und setze die Spaltennamen explizit auf ['Account Manager', 'Team', 'Abteilung']. 
        Schritt 3: Ermittle, welcher Abteilung Account Manager "Max Mustermann" zugeordnet ist.
        Schritt 4: Lade die Daten aus dem Tabellenblatt 'Abteilungsziele'
        Schritt 5: Ermittle 'Zielgröße', 'Durchschnittliche Schadenquote', 'Abteilungsziel erreicht (Ja/Nein)' für die Abteilung von Max Mustermann. Zielgrößen und Schadenquoten werden in Prozent angegeben.
        Schritt 6: Gib deine Ergebnisse klar und präzise aus.
        """,
        """
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe solange Du nicht explizit aufgefordert wirst.
        Schritt 1: Gib aus: "Zielart 2 Teamziele: \nDatengrundlage:"
        Schritt 2: Lade die Daten aus dem Tabellenblatt 'Teamzuordnung' und setze die Spaltennamen explizit auf ['Account Manager', 'Team', 'Abteilung']. 
        Schritt 3: Ermittle, welchem Team Account Manager "Max Mustermann" zugeordnet ist.
        Schritt 4: Lade die Daten aus dem Tabellenblatt 'Teamziele Bestand' und setze die Spaltennamen explizit auf ['Team', 'Ist Bestand Summe', 'Vorjahr Bestand Summe', 'Teamziel Bestand erreicht (Ja/Nein)']. 
        Schritt 5: Ermittle 'Vorjahr Bestand Summe', 'Ist Bestand Summe' und 'Teamziel Bestand erreicht (Ja/Nein)' für das Team von Max Mustermann aus. 
        Schritt 6: Lade die Daten aus dem Tabellenblatt 'Teamziele Neugeschäft' und setze die Spaltennamen explizit auf ['Team', 'Ist Neu-/Mehrgeschäft Summe', 'Vorjahr Neu-/Mehrgeschäft Summe', 'Teamziel Neugeschäft erreicht (Ja/Nein)'].
        Schritt 7: Ermittle 'Vorjahr Neu-/Mehrgeschäft Summe', 'Ist Neu-/Mehrgeschäft Summe' und 'Teamziel Neugeschäft erreicht (Ja/Nein)' für das Team von Max Mustermann aus. 
        Schritt 8: Gib deine Ergebnisse klar und präzise aus.
        """,
        """
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe solange Du nicht explizit aufgefordert wirst.
        Schritt 1: Gib aus: "Zielart 3 Persönliche Ziele - Messgröße Bestandsziele: \nDatengrundlage:"
        Schritt 2: Lade die Daten aus Tabelle 'Pers_Ziele_Bestand' in Tabellenblatt 'Pers Ziele Bestand' 
        Schritt 3: Filtere die Daten auf die Makler Accounts von Account Manager 'Max Mustermann'. Wieviele Makler sind das?
        Schritt 4: Ermittle, wieviele Makler mit 'Branchensegment' "Privat" oder "SMC" das persönliche Ziel Bestand erreichen.
        Schritt 5: Ermittle, wieviele Makler mit 'Branchensegment' "MidCorp" das persönliches Ziel Bestand erreichen.
        Schritt 6: Ermittle, wie hoch das Bestandsvolumen des Maklerportfolios aktuell is und wie hoch es im Vorjahr war.
        Schritt 7: Gib deine Ergebnisse klar und präzise aus.
        """,
        """
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe solange Du nicht explizit aufgefordert wirst.
        Schritt 1: Gib aus: "Zielart 3 Persönliche Ziele - Messgröße Neu-/Mehrgeschäftsziele: \nDatengrundlage:"
        Schritt 2: Lade die Daten aus Tabelle 'Pers_Ziele_Neugeschäft' in Tabellenblatt 'Pers Ziele Neugeschäft' 
        Schritt 3: Filtere die Daten auf die Makler Accounts von Account Manager 'Max Mustermann'. Wieviele Makler sind das? 
        Schritt 4: Ermittle, wieviele Makler mit 'Branchensegment' "Privat" oder "SMC" das persönliche Ziel Neu-/Mehrgeschäft erreichen. 
        Schritt 5: Ermittle, wieviele Makler mit 'Branchensegment' "MidCorp" das persönliches Ziel Neu-/Mehrgeschäft erreichen. 
        Schritt 6: Ermittle, wie hoch das Neu-/Mehrgeschäftsvolumen des Maklerportfolio aktuell is und wie hoch es im Vorjahr war. 
        Schritt 7: Gib deine Ergebnisse klar und präzise aus.
        """,
        """
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe solange Du nicht explizit aufgefordert wirst.
        Schritt 1: Gib aus: "Zielart 3 Persönliche Ziele - Produktive Makler: \nDatengrundlage:"
        Schritt 2: Lade die Daten aus Tabelle 'Pers_Ziele_Produktive_Makler' in Tabellenblatt 'Persönl Ziele Produktive Makler' 
        Schritt 3: Werte aus, wieviele Makler Accounts dem Account Manager 'Max Mustermann' zugeordnet sind. 
        Schritt 4: Werte aus, wieviele davon das persönliche Ziel produktive Makler erreichen.
        Schritt 5: Gib deine Ergebnisse klar und präzise aus.
        """
        ]
    
    with app.app_context():
        return run_prompts_with_temp_thread("target_analyze", prompt_steps, data_steps)

def get_abteilungsziele():
    prompt = """
        Befolge zur Ermittlung des Ergebnisses folgende Schritte, erzeuge aber keine Ausgabe für die Schritte.
        Schritt 1: Lade die Daten aus dem Tabellenblatt 'Teamzuordnung' und setze die Spaltennamen explizit auf ['Account Manager', 'Team', 'Abteilung']. 
        Schritt 2: Werte aus, welcher Abteilung Account Manager "Max Mustermann" zugeordnet ist
        Schritt 3: Lade die Daten aus dem Tabellenblatt 'Abteilungsziele'
        Schritt 4: Filtere die Daten auf die Abteilung von Max Mustermann.
        Schritt 5: Werte 'Zielgröße', 'Durchschnittliche Schadenquote', 'Abteilungsziel erreicht (Ja/Nein)' aus. Zielgrößen und Schadenquoten werden in Prozent angegeben.
        """
    
    # Schritt 1: Ausgabe
    output_step_1 = "Zielart 1 Abteilungsziele: \nDatengrundlage:"
    
    # Schritt 2: Lade die Daten aus dem Tabellenblatt 'Teamzuordnung'
    file_path = 'the file path'
    team_assignment_df = pd.read_excel(file_path, sheet_name='Teamzuordnung')
    
    # Setze die Spaltennamen
    team_assignment_df.columns = ['Account Manager', 'Team', 'Abteilung']
    
    # Zeige die ersten paar Zeilen des DataFrames an, um sicherzustellen, dass die Daten korrekt geladen wurden
    team_assignment_df.head()
    
    # Schritt 3: Ermittle, welcher Abteilung Account Manager "Max Mustermann" zugeordnet ist.
    max_mustermann_abteilung = team_assignment_df.loc[team_assignment_df['Account Manager'] == 'Max Mustermann', 'Abteilung'].values[0]
    max_mustermann_abteilung
    
    # Schritt 3: Ermittle, welcher Abteilung Account Manager "Max Mustermann" zugeordnet ist.
    max_mustermann_abteilung = team_assignment_df.loc[team_assignment_df['Account Manager'] == 'Max Mustermann', 'Abteilung'].values[0]
    max_mustermann_abteilung
    
    # Schritt 4: Lade die Daten aus dem Tabellenblatt 'Abteilungsziele'
    department_goals_df = pd.read_excel(file_path, sheet_name='Abteilungsziele')
    
    # Zeige die ersten paar Zeilen des DataFrames an, um sicherzustellen, dass die Daten korrekt geladen wurden
    department_goals_df.head()
    
    # Schritt 5: Ermittle 'Zielgröße', 'Durchschnittliche Schadenquote', 'Abteilungsziel erreicht (Ja/Nein)' für die Abteilung von Max Mustermann
    max_mustermann_goals = department_goals_df.loc[department_goals_df['Abteilung'] == max_mustermann_abteilung]
    
    # Extrahiere die benötigten Werte
    zielgroesse = max_mustermann_goals['Zielgröße'].values[0] * 100  # in Prozent
    durchschnittliche_schadenquote = max_mustermann_goals['Durchschnittliche Schadenquote'].values[0] * 100  # in Prozent
    abteilungsziel_erreicht = max_mustermann_goals['Abteilungsziel erreicht (Ja/Nein)'].values[0]
    
    zielgroesse, durchschnittliche_schadenquote, abteilungsziel_erreicht
    
    # Schritt 6: Gib die Ergebnisse klar und präzise aus.
    results = {
        "Zielgröße (%)": zielgroesse,
        "Durchschnittliche Schadenquote (%)": durchschnittliche_schadenquote,
        "Abteilungsziel erreicht": abteilungsziel_erreicht
    }
    
    """
    with app.app_context():
        return run_prompt_with_code_interpreter_thread("get_abteilungsziele", prompt)
    """
    
def get_abteilung():
    prompt = """
        Schritt 1: Lade die Daten aus dem Tabellenblatt 'Teamzuordnung' und setze die Spaltennamen explizit auf ['Account Manager', 'Team', 'Abteilung']. 
        Schritt 2: Ermittle, welcher Abteilung Account Manager "Max Mustermann" zugeordnet ist. Gib die Bezeichnung der Abteilung aus.
        """
    
    with app.app_context():
        return run_prompt_with_code_interpreter_thread("get_team", prompt)
        
def get_team():
    prompt = """
        Schritt 1: Lade die Daten aus dem Tabellenblatt 'Teamzuordnung' und setze die Spaltennamen explizit auf ['Account Manager', 'Team', 'Abteilung']. 
        Schritt 2: Ermittle, welchem Team Account Manager "Max Mustermann" zugeordnet ist. Gib die Bezeichnung des Teams aus.
        """
    
    with app.app_context():
        return run_prompt_with_code_interpreter_thread("get_team", prompt)
        
def get_teamziele():
    prompt = """
        Ermittle die Definition für die Zielart 2 Teamziele und wende diese Definitionen auf die vorliegenden Maklervertrieb Zahlen an. 
        Erstelle daraus eine Auflistung der Kennzahlen mit ihrem aktuellen Erreichungsgrad! Antworte möglichst detailliert, da deine Antwort in anderen Abfragen als Input weiterverwendet werden soll. 
        Stelle sicher, dass sämtliche Ergebnisse mathematisch korrekt sind.'
        """
    
    with app.app_context():
        return run_prompt_with_code_interpreter_thread("get_teamziele", prompt)
        
def get_bestandsziele():
    prompt = """
        Ermittle die Definition für die Messgröße Bestandsziele innerhalb der Zielart 3 Persönliche Ziele und wende diese Definitionen auf die vorliegenden Maklervertrieb Zahlen an. 
        Erstelle daraus eine Auflistung der Makler, die diese Zielvorgaben erreichen! Antworte möglichst detailliert, da deine Antwort in anderen Abfragen als Input weiterverwendet werden soll. 
        Stelle sicher, dass sämtliche Ergebnisse mathematisch korrekt sind.
        """
    
    with app.app_context():
        return run_prompt_with_code_interpreter_thread("get_bestandsziele", prompt)

def get_neugeschaeftsziele():
    prompt = """
        Ermittle die Definition für die Messgröße Neu- Mehrgeschäft innerhalb der Zielart 3 Persönliche Ziele und wende diese Definition auf die vorliegenden Maklervertrieb Zahlen an. 
        Erstelle daraus eine Auflistung der Makler, die diese Zielvorgaben erreichen! Antworte möglichst detailliert, da deine Antwort in anderen Abfragen als Input weiterverwendet werden soll. 
        Stelle sicher, dass sämtliche Ergebnisse mathematisch korrekt sind.
        """
    
    with app.app_context():
        return run_prompt_with_code_interpreter_thread("get_bestandsziele", prompt)
        
def get_produktive_makler():
    prompt = """
        Befolge zur Ermittlung des Ergebnisses folgende Schritte, erzeuge aber keine Ausgabe für die Schritte.
        Schritt 1: Lade die Daten aus Tabelle 'Pers_Ziele_Produktive_Makler' in Tabellenblatt 'Persönl Ziele Produktive Makler' 
        Schritt 2: Werte aus, wieviele Makler Accounts dem Account Manager 'Max Mustermann' zugeordnet sind. 
        Schritt 3: Werte aus, wieviele davon das persönliche Ziel produktive Makler erreichen.
        """
    
    with app.app_context():
        return run_prompt_with_code_interpreter_thread("get_produktive_makler", prompt)
        
def target_gap(file_path):
    logger.info('target_gap function triggered')
        
    output_template_final = {
            "Ich habe Dein Maklerportfolio analysiert und Zielkorrelationen berücksichtig um deine persönlichen Ziele effizient zu erreichen."
            "\n- Dem Makler (Accountname, Strukturnummer MSN06) fehlen noch XX TEUR im Bestandsgeschäft (Privat + SMC) um das VJ Ziel zu erreichen. Gleichzeitig wird er dadurch produktiv."
            "\n- Dem Makler (Accountname, Strukturnummer MSN06) fehlen noch XX TEUR im Bestandsgeschäft (MC) um das VJ Ziel zu erreichen. Gleichzeitig wird er dadurch produktiv."
            "\n- Dem Makler(Accountname, Strukturnummer MSN06) benötigt noch ein Neu-/Mehrgeschäft (Privat+SMC) von XX TEUR um das VJ Ziel zu erreichen. Gleichzeitig wird er dadurch produktiv."
            "\n\nDurch die Steigerung des Bestandsgeschäfts und Mehr-/Neubeitrag dieser Makler erreichst Du effizient und optimal Deine Ziele:"
            "\nBestandsziele:"
            "\n- 5 von 5 Maklern werden den Bestand (Privat + SMC) im Vergleich zum Vorjahr steigern. "
            "\n- X von 8 Maklern werden den Bestand (Firmen MC) im Vergleich zum Vorjahr steigern."
            "\nIngesamt wird Dein Maklerportfolio ein Bestandsvolument von X TEUR erreichen, im VJ wurden X TEUR erreicht."
            "\n\nNeu-/Mehrgeschäftsziele:"
            "\n- 7 von 8 Makern werden das Neu/Mehrgeschäft(Privat + SMC) im Vergleich zum Vorjahr steigern. "
            "\n- 7 von 8 Makern werden das Neu/Mehrgeschäft(Firmen MC) im Vergleich zum Vorjahr steigern." 
            "\n\nIngesamt wird Dein Maklerportfolio ein Neu-/Mehrgeschäft von X TEUR haben, im VJ wurden X TEUR erreicht."
            "\n\nProduktive Makler: "
            "\n- 7 von 7 Maklern werden produktiv."
        }

    result3 = get_bestandsziele()
    result4 = get_neugeschaeftsziele()
    result5 = get_produktive_makler()
    
    data_steps = []
    
    prompt_steps = [
            f'Hier sind Definitionen und Ergebnisse für die persönliche Zielerreichung des Maklerbetreuers auf Ebene der einzelnen Makler: \nBestandsziele: {result3} \nNeu-/Mehrgeschäftsziele: {result4} \n Ziel Produktive Makler: {result5} \nBeantworte mir in der Folge Fragen auf Basis dieser Definitionen und Daten.',
            f'Ermittle, wie die einzelnen Makler die Ziele (Bestand, Neu-/Mehrgeschäft, Produktiver Makler) am effizientesten erreichen können, falls diese noch nicht erreicht wurden. Konzentriere dich auf diejenigen Kennzahlen, die aufgrund einer Zielkorrelation den größten Effekt auf die Zielerreichung der meisten Ziele haben. Stelle sicher, dass sämtliche Ergebnisse mathematisch korrekt sind.',
            f'Nimm an, die untersuchten Makler verbessern ihre Messgrößen entsprechend. Wieviele Makler werden dann ihren Bestand im Vergleich zum Vorjahr steigern? Wieviele Makler werden dadurch produktiv? Stelle sicher, dass sämtliche Ergebnisse mathematisch korrekt sind. \nHier ein Beispiel: - Dem Makler MaklerCorp fehlen noch 1000 € im Bestandsgeschäft (Privat + SMC) um das Vorjahres-Ziel zu erreichen. Gleichzeitig erreicht er dadurch das Ziel Produktiver Makler.',
            f'Fasse deine Ergebnisse entsprechend folgendem Beispiel zusammen: {output_template_final}'
        ]
    
    with app.app_context():
        return run_prompts_with_temp_thread("target_analyze", prompt_steps, data_steps)

def team_analyze():
    logger.info('team_analyze function triggered')
    return 'Max Mustermann hat eine Performance = 65%, Dieter Hans hat eine Performance = 82%, Ulrich Mark hat eine Performance = 85% '

def create_appointment_task():
    logger.info('create_appointment_task function triggered')
    return '05.10. 14:15 ; 07.10 16:35'
    
def create_appointment():
    return 'Termin wurde im Kalender hinterlegt.'
    
def productive_broker_analyze(path):
    logger.info('productive_broker_analyze function triggered')
    
    data_steps =[
        """
        Durchlaufe folgende Schritte. Erzeuge keine Ausgabe außer dem Endergebnis. 
        Schritt 1: Gib aus: "Zielart 3 Persönliches Ziel: Produktive Makler: \nDatengrundlage:" 
        Schritt 2: Lade die Daten aus Tabelle 'Pers_Ziele_Produktive_Makler' in Tabellenblatt 'Persönl Ziele Produktive Makler' 
        Schritt 3: Werte aus, wieviele Makler Accounts dem Account Manager 'Max Mustermann' zugeordnet sind. 
        Schritt 4: Werte aus, wieviele davon das persönliche Ziel produktive Makler erreichen. 
        Schritt 5: Gib deine ermittelten Ergebnisse aus.
        """
        ] 
    
    prompt_steps = [
        """
        Erstelle eine Übersicht entsprechend folgendem Musterbeispiel: 
        "Zielerreichung: \n- x von y Maklern sind bereits produktiv."
        Begrenze deine Ausgabe auf die angegebene Übersicht.
        Nutze dazu folgende Daten:
        """
        ]
    
    with app.app_context():
        return run_prompts_with_temp_thread("productive_broker_analyze", prompt_steps, data_steps)
    

def create_output(run, tool_calls, path, thread):
    tool_outputs = []
    for tool in tool_calls:
        if tool.function.name == "team_analyze":
            result = team_analyze()
            logger.info('providing team_analyze function results')
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
            run = client.beta.threads.runs.submit_tool_outputs(
                thread_id=thread.id,
                run_id=run.id,
                tool_outputs=tool_outputs,
                stream=True,
            )
            logger.info('Tool outputs submitted successfully.')
            return run
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
                    response.append(formatted_content)
                    #response.append({"role": "assistant", "content": formatted_content})
                    logger.info(f'Processed content 1: {formatted_content}')
                    break  # Only handle the first relevant content
    return response

@app.route('/', methods=['GET', 'POST'])
def home():
    if 'assistant_id' not in session:
        assistant = initialize_assistant_for_session()
        session['assistant_id'] = assistant.id
    else:
        assistant_id = session['assistant_id']
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
    global assistant
    assistant = None
    global thread
    thread = None
    global temp_assistant
    temp_assistant = None
    global temp_thread
    temp_thread = None
    global team
    team = ""
    global abteilung
    abteilung = ""
    session.clear()
    return redirect(url_for('home'))
    
# In-memory store for messages (simple implementation)
streaming_responses = {}
combined_message = ""
mock_user = "Max Mustermann"
data_response = ""
team = ""
abteilung = ""

# Function to handle streaming responses from OpenAI
def handle_streaming_response(mystream, user_id, prompt, assistant_id, multiple):
    global analysis_result
    global thread
    global combined_message
    suggestions = []
    global data_response

    try:
        if mystream is None:
            #assistant = client.beta.assistants.retrieve(assistant_id)
            if thread is None: thread = client.beta.threads.create()
            thread_message = client.beta.threads.messages.create(
                thread_id=thread.id,
                role="user",
                content=prompt,
            )
    
            stream = client.beta.threads.runs.create(
                thread_id=thread.id,
                assistant_id=assistant_id,
                stream=True,
            )
    
            path = os.path.join(base_dir, 'uploads', 'docs', 'maklervertrieb_zahlen_v0.3.xlsx')
    
            task_completed.clear()  # Reset task event
            analysis_result = {"status": "running"}
    
            logger.info(f'Stream started for assistant ID: {assistant.id}')
        else:
            stream = mystream
        
        if multiple is None:
            combined_message = ""
            
        if prompt is not None:
            suggestions = generate_follow_up_questions(prompt)
        
        for chunk in stream:

            # Handle initial events
            if chunk.event == 'thread.created':
                logger.info(f"Thread created with ID: {chunk.data.id}")
            elif chunk.event in ['thread.run.created', 'thread.run.queued', 'thread.run.in_progress', 'thread.run.step.created', 'thread.run.step.in_progress']:
                logger.info(f"Event: {chunk.event}, Data: {chunk.data}")
            elif chunk.event == 'thread.message.delta':
                for block in chunk.data.delta.content:
                    if block.type == 'text':
                        content = block.text.value
                        combined_message += content
                        if user_id in streaming_responses:
                            streaming_responses[user_id].append({"role": "assistant", "content": combined_message, "is_streaming": True, "suggestions": []})
                        else:
                            streaming_responses[user_id] = [{"role": "assistant", "content": combined_message, "is_streaming": True, "suggestions": []}]
            elif chunk.event == 'thread.message.completed':
                logger.info(f"Message completed with content: {chunk.data.content}")
                # Mark the end of message and indicate final message
                data_response = str(chunk.data.content)
                if user_id in streaming_responses:
                    if multiple is None:
                        streaming_responses[user_id].append({"role": "assistant", "content": combined_message, "is_streaming": False, "suggestions": suggestions})
                    else:
                        combined_message += "\n\n"
                else:
                    streaming_responses[user_id] = [{"role": "assistant", "content": combined_message, "is_streaming": False, "suggestions": suggestions}]
            elif chunk.event == 'thread.run.requires_action':
                # Handle required tool calls
                tool_calls = chunk.data.required_action.submit_tool_outputs.tool_calls
                create_output(chunk.data, tool_calls, path, thread)
                if user_id in streaming_responses:
                    streaming_responses[user_id].append({"role": "assistant", "content": combined_message, "is_streaming": False, "suggestions": suggestions})
                else:
                    streaming_responses[user_id] = [{"role": "assistant", "content": combined_message, "is_streaming": False, "suggestions": suggestions}]
                logger.info("Tool outputs created")
            elif chunk.event == 'thread.run.completed':
                logger.info("Thread run completed")

        analysis_result['messages'] = combined_message
        analysis_result['suggestions'] = suggestions
        logger.info("Task completed successfully")

        task_completed.set()

    except Exception as e:
        logger.error(f"Error during OpenAI streaming: {str(e)}", exc_info=True)
        if user_id in streaming_responses:
            streaming_responses[user_id].append({"role": "assistant", "content": f"Error: {str(e)}"})
        else:
            streaming_responses[user_id] = [{"role": "assistant", "content": f"Error: {str(e)}"}]

@app.route('/chat', methods=['POST'])
def chat():
    global user_id
    global assistant
    global team
    global abteilung
    
    if team == "": team = get_team()
    if abteilung == "": abteilung = get_abteilung()
    
    user_input = request.json.get('user_input')
    logger.info(f"Received user input: {user_input}")
    user_id = str(request.remote_addr)  # Using the client's IP as a simple user identifier
    
    #assistant = client.beta.assistants.retrieve("asst_7Hx0vFUQZDlJd1aSRm8HjtjR")
    assistant_id = assistant.id
    session['assistant_id'] = assistant_id
    
    # Initialize the user's response collection
    streaming_responses[user_id] = []
    
    # Start the streaming response in a separate thread
    logger.info("Starting background task")
    threading.Thread(target=handle_streaming_response, args=(None, user_id, user_input, assistant_id, None)).start()
    
    return jsonify({"status": "streaming", "user_id": user_id})

@app.route('/stream/<user_id>')
def stream(user_id):
    def message_generator():
        while True:
            if user_id in streaming_responses and streaming_responses[user_id]:
                message = streaming_responses[user_id].pop(0)
                yield f"data: {json.dumps(message)}\n\n"
                if not message.get('is_streaming', True):
                    break
            #time.sleep(0.1)  # Reducing sleep to improve responsiveness

    return Response(stream_with_context(message_generator()), content_type='text/event-stream')

if __name__ == '__main__':
    logger.info('Main executed')
    #app.run(host='0.0.0.0', port=8080, debug=False)
    app.run(host='0.0.0.0', port=8080, threaded=True, use_reloader=False)