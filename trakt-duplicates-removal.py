import json
import requests
import webbrowser
import time
import os
from dotenv import load_dotenv

load_dotenv()

# User-defined settings
CLIENT_ID = os.getenv('CLIENT_ID') or 'your_client_id'
CLIENT_SECRET = os.getenv('CLIENT_SECRET') or 'your_client_secret'
USERNAME = os.getenv('USERNAME') or 'your_username'

TYPES = ['movies', 'episodes']
KEEP_PER_DAY = False  # Set to True to keep one entry per day

# Constants
TRAKT_API = 'https://api.trakt.tv'
TOKEN_FILE = 'trakt_auth.json'

session = requests.Session()

def load_token():
    """Load authentication token from file and check if it needs refreshing."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as file:
            token_data = json.load(file)
        
        if 'expires_at' in token_data and token_data['expires_at'] < time.time():
            print("🔄 Token expired, refreshing...")
            return refresh_access_token(token_data)
        
        return token_data
    return None

def save_token(token_data):
    """Save authentication token to file with expiry timestamp."""
    token_data['expires_at'] = time.time() + token_data['expires_in']  # Set expiry time
    with open(TOKEN_FILE, 'w') as file:
        json.dump(token_data, file)

def refresh_access_token(token_data):
    """Refresh the access token using the refresh token."""
    response = session.post(f'{TRAKT_API}/oauth/token', json={
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'refresh_token': token_data['refresh_token'],
        'grant_type': 'refresh_token'
    })
    
    if response.status_code == 200:
        new_token_data = response.json()
        save_token(new_token_data)
        print("✅ Token refreshed successfully.")
        return new_token_data
    else:
        print("❌ Failed to refresh token, please authenticate again.")
        return authenticate()

def authenticate():
    """Handle initial authentication with Trakt API."""
    response = session.post(f'{TRAKT_API}/oauth/device/code', json={'client_id': CLIENT_ID})
    if response.status_code != 200:
        print(f"❌ Failed to get device code: {response.status_code}")
        return None
    
    data = response.json()
    print(f"🔗 Open this link in your browser: {data['verification_url']}")
    print(f"🔢 Enter this code: {data['user_code']}")
    webbrowser.open(data['verification_url'])
    
    start_time = time.time()
    timeout = data['expires_in']
    interval = data.get('interval', 5)
    
    while time.time() - start_time < timeout:
        time.sleep(interval)
        
        token_response = session.post(f'{TRAKT_API}/oauth/device/token', json={
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'code': data['device_code'],
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'
        })
        
        if token_response.status_code == 200:
            token_data = token_response.json()
            save_token(token_data)
            print("✅ Authentication successful!")
            return token_data
        elif token_response.status_code == 400:
            pass
        elif token_response.status_code == 429:
            print("⏳ Slow down, waiting a bit longer...")
            time.sleep(interval)
        else:
            print(f"❌ Authentication failed with status code: {token_response.status_code}")
            print(token_response.text)
            break
    
    print("⏰ Authentication timed out. Please try again.")
    return None

def make_authenticated_request(url, method='GET', data=None):
    """Make an authenticated API request, refreshing the token if necessary."""
    token_data = load_token()
    if not token_data:
        token_data = authenticate()
    
    headers = {
        'Authorization': f"Bearer {token_data['access_token']}",
        'trakt-api-version': '2',
        'trakt-api-key': CLIENT_ID
    }
    
    response = session.request(method, url, headers=headers, json=data)
    
    if response.status_code == 401:
        print("🔄 Token expired, refreshing and retrying request...")
        token_data = refresh_access_token(token_data)
        headers['Authorization'] = f"Bearer {token_data['access_token']}"
        response = session.request(method, url, headers=headers, json=data)
    
    return response

def remove_duplicate(history, type):
    """Remove duplicate entries from watch history."""
    entry_type = 'movie' if type == 'movies' else 'episode'
    entries, duplicates = {}, []

    for entry in history[::-1]:
        entry_id = entry[entry_type]['ids']['trakt']
        watched_date = entry['watched_at'].split('T')[0]
        
        if entry_id in entries:
            if not KEEP_PER_DAY or watched_date == entries[entry_id]:
                duplicates.append(entry)
        else:
            entries[entry_id] = watched_date

    if duplicates:
        response = make_authenticated_request(f'{TRAKT_API}/sync/history/remove', method='POST', data={'ids': [entry['id'] for entry in duplicates]})
        if response.status_code != 200:
            print(f"❌ Failed to remove duplicates. Response: {response.status_code} - {response.text}")
        else:
            return {"length":len(duplicates), "duplicates":duplicates}, len(entries)
    return {"length":0, "duplicates":[]}, len(entries)

if __name__ == '__main__':
    token_data = load_token()
    if not token_data:
        token_data = authenticate()

    removed = {}
    counts = {}
    
    for type in TYPES:
        # print(f"🔍 Scanning {type}...")
        history = make_authenticated_request(f'{TRAKT_API}/users/{USERNAME}/history/{type}?page=1&limit=100000').json()
        removed[type], counts[type] = remove_duplicate(history, type)

    if any(x['length'] for x in removed.values()):
        types = [f"{v['length']} {k[:-1]+'s' if v['length'] > 1 else k[:-1]}" for k, v in removed.items() if v['length'] > 0]
        removed_text = ' and '.join(types)
        print(f"🗑️ Removed {removed_text} duplicate{'s' if sum(x['length'] for x in removed.values()) > 1 else ''}:")
        for type, rems in removed.items():
            if rems['length'] > 0:
                if len(types) > 1: print(f"{type.title()}:")
                content = list(set([x.get('show', x.get('movie')).get('title') for x in rems['duplicates']]))
                text = ', '.join(content)
                print(f"  {text}"[:50] + '...' if len(text) > 50 else f"  {text}")
    else:
        counts = ' and '.join([f"{counts[k]} {k}" for k in TYPES])
        print(f"👍 No duplicates in {counts}.")
