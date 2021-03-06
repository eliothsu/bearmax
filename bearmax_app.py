from flask import Flask
from flask import request, redirect
import json
import requests
import urllib
from webob import Response

from pymongo import MongoClient

import watson

from symptomchecker import SymptomChecker

FB_APP_TOKEN = 'EAAQ7z3BxdYgBAL3ZBOmSKkUZCS9NodoMynT2SGZCZCjEo671spa5qVwGkhTVLZBofZAtgxQJc4RIbp9aHbOVesY5jDZC6oKgr8bqzkO6ewklEDM2xC12gLDuEkmeXQNEDBPDQ4mtWY8yRp2uEc77rR0wRCGNIPV66Q4sicfKgMuVAZDZD' 
FB_ENDPOINT = 'https://graph.facebook.com/v2.6/me/{0}'
FB_MESSAGES_ENDPOINT = FB_ENDPOINT.format('messages')
FB_THREAD_SETTINGS_ENDPOINT = FB_ENDPOINT.format('thread_settings')

MONGO_DB_BEARMAX_DATABASE = 'bearmax'
MONGO_DB_BEARMAX_ENDPOINT = 'ds151707.mlab.com'
MONGO_DB_BEARMAX_PORT = 51707

MONGO_DB_USERNAME = 'bearmax'
MONGO_DB_PASSWORD = 'calhacks'

SYMPTOMS_THRESHOLD = 4

def connect():
    connection = MongoClient(
        MONGO_DB_BEARMAX_ENDPOINT,
        MONGO_DB_BEARMAX_PORT
    )
    handle = connection[MONGO_DB_BEARMAX_DATABASE]
    handle.authenticate(
        MONGO_DB_USERNAME,
        MONGO_DB_PASSWORD
    )
    return handle

app = Flask(__name__)
app.config['DEBUG'] = True
handle = connect()

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        if request.args.get('hub.verify_token') == 'bear':
            return request.args.get('hub.challenge')
        else:
            return 'Wrong validation token'
    elif request.method == 'POST':
        data = json.loads(request.data)['entry'][0]['messaging']
        for i in range(len(data)):
            event = data[i]
            if 'sender' in event:
                print('Event: {0}'.format(event))
                sender_id = event['sender']['id']
                if 'message' in event and 'is_echo' in event['message'] and event['message']['is_echo']:
                    pass
                elif handle.bot_users.find({'sender_id': sender_id}).count() == 0:
                    send_FB_text(sender_id, 'Hello! I am Bearmax, your personal healthcare companion.')
                    init_bot_user(sender_id)
                else:
                    sender_id_matches = [x for x in handle.bot_users.find({'sender_id': sender_id})]
                    if sender_id_matches:
                        bot_user = sender_id_matches[0]
                        apimedic_client = SymptomChecker()
                        handle_event(event, bot_user, apimedic_client)

    return Response()

def handle_event(event, bot_user, apimedic_client):
    if 'message' in event and 'text' in event['message']:
        message = event['message']['text']
        print('Message: {0}'.format(message))
        if message.isdigit():
            yob = int(message)
            set_age(bot_user, yob)
            send_FB_text(bot_user['sender_id'], 'Thank you. Please describe one of your symptoms.')
        elif 'quick_reply' in event['message']:
            handle_quick_replies(
                event['message']['quick_reply']['payload'],
                bot_user,
                apimedic_client
            )
        elif message in ['Male', 'Female']:
            pass
        else:
            natural_language_classifier, instance_id = watson.init_nat_lang_classifier(True)
            symptom, symptom_classes = watson.get_symptoms(message, natural_language_classifier, instance_id)
            print('Symptom: {0}, Symptom Classes: {1}'.format(symptom, symptom_classes))
            symptom_classes = ','.join([symptom_class['class_name'] for symptom_class in symptom_classes])
            send_FB_text(
                bot_user['sender_id'],
                'You seem to have a symptom known as \"{0}\". Is this true?'.format(symptom.lower()),
                quick_replies=yes_no_quick_replies(symptom, symptom_classes)
             )
    elif 'postback' in event:
        handle_postback(event['postback']['payload'], bot_user, apimedic_client)

def handle_postback(payload, bot_user, apimedic_client):
    if 'description' in payload:
        ailment_id = int(payload.split(':')[1])
        send_description(ailment_id, apimedic_client, bot_user)

def diagnose(apimedic_client, bot_user):
    diagnosis = apimedic_client.get_diagnosis(
        bot_user['symptoms'],
        bot_user['gender'],
        bot_user['year_of_birth']
    )
    for diag in diagnosis:
        name, specialisation = diag['Issue']['Name'], diag['Specialisation'][0]['Name']
        accuracy = diag['Issue']['Accuracy']
        if specialisation == 'General practice':
            specialisation = 'treatment'
        send_FB_text(bot_user['sender_id'], 'You have a {0}% chance of an ailment known as \"{1}\"'.format(accuracy, name.lower())) 
    ailment_id = diagnosis[0]['Issue']['ID']
    send_FB_buttons(
        bot_user['sender_id'],
        'You should seek {0} for your {1}'.format(specialisation.lower(), diagnosis[0]['Issue']['Name'].lower()),
        [{
            'type': 'postback',
            'title': 'Read more',
            'payload': 'description:{0}'.format(ailment_id)
        }]
    )
    reset_symptoms(bot_user)

def send_description(ailment_id, apimedic_client, bot_user):
    description = apimedic_client.get_description(ailment_id)
    for sentence in description['DescriptionShort'].split('. '):
        send_FB_text(bot_user['sender_id'], sentence)
    for sentence in description['TreatmentDescription'].split('. '):
        send_FB_text(bot_user['sender_id'], sentence)

def handle_quick_replies(payload, bot_user, apimedic_client):
    print('Payload: {0}'.format(payload))
    if 'Gender:' in payload:
        gender = payload.split(':')[1]
        set_gender(bot_user, gender)
        send_FB_text(bot_user['sender_id'], 'What year were you born?')
    elif 'Yes:' in payload:
        add_symptom(bot_user, payload.split(':')[1])
        bot_user = [x for x in handle.bot_users.find({'sender_id': bot_user['sender_id']})][0]
        if len(bot_user['symptoms']) >= SYMPTOMS_THRESHOLD:
            diagnose(apimedic_client, bot_user)
        else:
            proposed_symptoms = apimedic_client.get_proposed_symptoms(
                bot_user['symptoms'],
                bot_user['gender'],
                bot_user['year_of_birth']
            )
            symptom_names = [symptom['Name'] for symptom in proposed_symptoms if symptom['Name'] not in bot_user['symptoms_seen'] and symptom['Name'] != 'Fast, deepened breathing']
            symptom, symptom_classes = symptom_names[0], ','.join(symptom_names)

            send_FB_text(
                bot_user['sender_id'],
                'Alright. Do you also have a symptom known as \"{0}\"?'.format(symptom.lower()),
                quick_replies=yes_no_quick_replies(symptom, symptom_classes)
            )
    elif 'No:' in payload:
        symptom_classes = payload.split(':')[1].split(',')
        add_symptom_seen(bot_user, symptom_classes.pop(0))
        if not symptom_classes or symptom_classes == ['']:
            if bot_user['symptoms']:
               diagnose(apimedic_client, bot_user) 
            else:
                send_FB_text(
                    bot_user['sender_id'],
                    'I\'m sorry, but I was not able to diagnose you.'
                )
                reset_symptoms(bot_user)
        else:
            symptom, symptom_classes = symptom_classes[0], ','.join(symptom_classes)
            send_FB_text(
                bot_user['sender_id'],
                'Alright. Do you have symptom known as \"{0}\"?'.format(symptom.lower()),
                quick_replies=yes_no_quick_replies(symptom, symptom_classes)
            )

def yes_no_quick_replies(symptom, symptom_classes):
    return [
        {
            'content_type': 'text',
            'title': 'Yes',
            'payload': 'Yes:{0}'.format(symptom)
        },
        {
            'content_type': 'text',
            'title': 'No',
            'payload': 'No:{0}'.format(symptom_classes)
        }
    ]

def set_gender(bot_user, gender):
    handle.bot_users.update(
        {'sender_id': bot_user['sender_id']},
        {
            '$set': {
                'gender': gender,
            }
        }
    )

def add_symptom(bot_user, symptom):
    handle.bot_users.update(
        {'sender_id': bot_user['sender_id']},
        {
            '$set': {
                'symptoms': bot_user['symptoms'] + [symptom]
            }
        }
    )


def add_symptom_seen(bot_user, symptom):
    handle.bot_users.update(
        {'sender_id': bot_user['sender_id']},
        {
            '$set': {
                'symptoms_seen': bot_user['symptoms_seen'] + [symptom]
            }
        }
    )

def reset_symptoms(bot_user):
    handle.bot_users.update(
        {'sender_id': bot_user['sender_id']},
        {
            '$set': {
                'symptoms': [],
                'symptoms_seen': []
            }
        }
    )

def init_bot_user(sender_id):
    send_FB_text(
        sender_id,
        'What is your gender?',
        quick_replies=[
            {
                'content_type': 'text',
                'title': 'Male',
                'payload': 'Gender:male'
            },
            {
                'content_type': 'text',
                'title': 'Female',
                'payload': 'Gender:female'
            }
        ]
    )
    handle.bot_users.insert({
        'sender_id': sender_id,
        'symptoms': [],
        'symptoms_seen': []
    })

def set_age(bot_user, yob):
    print('Setting yob to {0}'.format(yob))
    handle.bot_users.update(
        {'sender_id': bot_user['sender_id']},
        {
            '$set': {
                'year_of_birth': yob
            }
        }
    )

def send_FB_message(sender_id, message):
    fb_response = requests.post(
        FB_MESSAGES_ENDPOINT,
        params={'access_token': FB_APP_TOKEN},
        data=json.dumps(
            {
                'recipient': {
                    'id': sender_id
                },
                'message': message
            }
        ),
        headers={'content-type': 'application/json'}
    )
    if not fb_response.ok:
        app.logger.warning('Not OK: {0}: {1}'.format(
            fb_response.status_code,
            fb_response.text
        ))


def send_FB_text(sender_id, text, quick_replies=None):
    message = {'text': text}
    if quick_replies:
        message['quick_replies'] = quick_replies
    return send_FB_message(
        sender_id,
        message
    )


def send_FB_buttons(sender_id, text, buttons):
    return send_FB_message(
        sender_id,
        {
            'attachment': {
                'type': 'template',
                'payload': {
                    'template_type': 'button',
                    'text': text,
                    'buttons': buttons
                }
            }
        }
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
