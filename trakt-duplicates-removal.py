import json
import requests
import webbrowser
import time
import os
import sys
from collections import defaultdict
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Constants
TRAKT_API = 'https://api.trakt.tv'
TOKEN_FILE = 'trakt_auth.json'
CLIENT_ID = os.getenv('CLIENT_ID') or 'your_client_id'
CLIENT_SECRET = os.getenv('CLIENT_SECRET') or 'your_client_secret'
USERNAME = os.getenv('USERNAME') or 'your_username'
KEEP_PER_DAY = False  # Set to True to keep one entry per day


class TraktClient:
    def __init__(self):
        self.session = requests.Session()
        self.token_data = self.load_token()

    def load_token(self):
        """Load authentication token from file."""
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, 'r') as file:
                    token_data = json.load(file)
                
                if 'expires_at' in token_data and token_data['expires_at'] < time.time():
                    print("🔄 Token expired, refreshing...")
                    return self.refresh_access_token(token_data)
                
                return token_data
            except (json.JSONDecodeError, IOError):
                return None
        return None

    def save_token(self, token_data):
        """Save authentication token to file."""
        token_data['expires_at'] = time.time() + token_data['expires_in']
        with open(TOKEN_FILE, 'w') as file:
            json.dump(token_data, file)
        self.token_data = token_data

    def authenticate(self):
        """Handle initial authentication with Trakt API."""
        print("🔐 Authenticating with Trakt...")
        response = self.session.post(f'{TRAKT_API}/oauth/device/code', json={'client_id': CLIENT_ID})
        if response.status_code != 200:
            print(f"❌ Failed to get device code: {response.status_code}")
            return None
        
        data = response.json()
        print(f"🔗 Open this link: {data['verification_url']}")
        print(f"🔢 Enter code: {data['user_code']}")
        webbrowser.open(data['verification_url'])
        
        start_time = time.time()
        timeout = data['expires_in']
        interval = data.get('interval', 5)
        
        while time.time() - start_time < timeout:
            time.sleep(interval)
            
            token_response = self.session.post(f'{TRAKT_API}/oauth/device/token', json={
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET,
                'code': data['device_code'],
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'
            })
            
            if token_response.status_code == 200:
                token_data = token_response.json()
                self.save_token(token_data)
                print("✅ Authentication successful!")
                return token_data
            elif token_response.status_code == 429:
                time.sleep(interval)
            elif token_response.status_code != 400:
                print(f"❌ Auth failed: {token_response.status_code}")
                break
        
        print("⏰ Authentication timed out.")
        return None

    def refresh_access_token(self, token_data):
        """Refresh the access token."""
        response = self.session.post(f'{TRAKT_API}/oauth/token', json={
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'refresh_token': token_data['refresh_token'],
            'grant_type': 'refresh_token'
        })
        
        if response.status_code == 200:
            new_token_data = response.json()
            self.save_token(new_token_data)
            print("✅ Token refreshed.")
            return new_token_data
        
        print("❌ Refresh failed. Re-authenticating...")
        return self.authenticate()

    def request(self, url, method='GET', data=None):
        """Make an authenticated API request."""
        if not self.token_data:
            self.token_data = self.authenticate()
            if not self.token_data:
                raise Exception("Authentication required")

        headers = {
            'Authorization': f"Bearer {self.token_data['access_token']}",
            'trakt-api-version': '2',
            'trakt-api-key': CLIENT_ID
        }
        
        response = self.session.request(method, url, headers=headers, json=data)
        
        if response.status_code == 401:
            print("🔄 Token expired, refreshing...")
            self.token_data = self.refresh_access_token(self.token_data)
            headers['Authorization'] = f"Bearer {self.token_data['access_token']}"
            response = self.session.request(method, url, headers=headers, json=data)
        
        return response


class DuplicateCleaner:
    def __init__(self, client):
        self.client = client
        self.stats = defaultdict(lambda: {"count": 0, "removed": []})

    def run(self):
        # Silent mode - only print final summary
        for media_type in ['movies', 'episodes']:
            history = self._get_history(media_type)
            if not history:
                continue
                
            duplicates = self._find_duplicates(history, media_type)
            if duplicates:
                self._remove_duplicates(duplicates)
                self.stats[media_type]["count"] = len(duplicates)
                self.stats[media_type]["removed"] = duplicates
        
        self._print_summary()

    def _get_history(self, media_type):
        url = f'{TRAKT_API}/users/{USERNAME}/history/{media_type}?page=1&limit=100000&extended=full'
        response = self.client.request(url)
        if response.status_code == 200:
            return response.json()
        # Only print errors
        print(f"❌ Failed to fetch {media_type} history: {response.status_code}")
        return []

    def _find_duplicates(self, history, media_type):
        entry_key = 'movie' if media_type == 'movies' else 'episode'
        grouped = defaultdict(list)
        
        # Group by Trakt ID
        for entry in history:
            try:
                tid = entry[entry_key]['ids']['trakt']
                grouped[tid].append(entry)
            except KeyError:
                continue

        duplicates = []
        
        for tid, entries in grouped.items():
            subgroups = [entries]
            
            # Subgroup by day if requested
            if KEEP_PER_DAY:
                day_map = defaultdict(list)
                for entry in entries:
                    date = entry['watched_at'].split('T')[0]
                    day_map[date].append(entry)
                subgroups = list(day_map.values())

            for group in subgroups:
                if len(group) > 1:
                    # Sort by date and ID (oldest/first-created first)
                    group.sort(key=lambda x: (x['watched_at'], x['id']))
                    
                    keeper = None
                    # Prioritize keeping 100% progress
                    for entry in group:
                        if entry.get('progress', 0) >= 100:
                            keeper = entry
                            break
                    
                    # Fallback to newest (last in sorted list) instead of oldest (first)
                    if not keeper:
                        keeper = group[-1]
                    
                    # Mark others as duplicates
                    for entry in group:
                        if entry['id'] != keeper['id']:
                            # Store title for reporting
                            entry['_title'] = entry[entry_key].get('title', 'Unknown')
                            if entry_key == 'episode':
                                show_title = entry.get('show', {}).get('title', '')
                                if show_title:
                                    entry['_title'] = f"{show_title}: {entry['_title']}"
                            duplicates.append(entry)
        
        return duplicates

    def _remove_duplicates(self, duplicates):
        # Batch removal if needed (API may have limits, but standard sync remove handles lists)
        ids_to_remove = [entry['id'] for entry in duplicates]
        response = self.client.request(
            f'{TRAKT_API}/sync/history/remove', 
            method='POST', 
            data={'ids': ids_to_remove}
        )
        
        if response.status_code not in [200, 201]:
            print(f"❌ Failed to remove duplicates: {response.text}")

    def _print_summary(self):
        total_removed = sum(s['count'] for s in self.stats.values())
        
        if total_removed == 0:
            print("✅ No duplicates found.")
        else:
            parts = []
            for mtype in ['movies', 'episodes']:
                count = self.stats[mtype]['count']
                if count > 0:
                    parts.append(f"{count} {mtype}")
            
            print(f"🗑️ Removed {total_removed} duplicates ({', '.join(parts)}):")
            
            # Gather all titles
            all_titles = []
            for data in self.stats.values():
                all_titles.extend([d['_title'] for d in data['removed']])
            
            # Deduplicate and sort
            titles = sorted(list(set(all_titles)))
            print("  " + ", ".join(titles))


def main():
    if not CLIENT_ID or CLIENT_ID == 'your_client_id':
        print("❌ Error: Please set CLIENT_ID and other env vars.")
        sys.exit(1)
        
    client = TraktClient()
    cleaner = DuplicateCleaner(client)
    cleaner.run()


if __name__ == '__main__':
    main()
