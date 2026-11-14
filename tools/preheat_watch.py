#!/usr/bin/env python3
"""Observe Vega via HA only. No vehicle commands or settings writes.

HA_TOKEN_FILE: a file containing an existing HA access token.
Or run inside the HA container with --existing-session (uses its existing owner
session without printing or storing credentials). --pull needs vega.read_sensors.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request

TARGETS = {'climate_target_temp': '22', 'climate_fan_speed': '8',
           'seat_heater_driver': '3', 'seat_heater_passenger': '0', 'steering_wheel_heater': '3',
           'seat_heater_rear_right': '3', 'climate_air_distribution': '2'}
IDS = [2801, 2805, 2806, 2906, 2907, 2912, 2913, 2918, 2902, 2924, 2925]
NAMES = list(TARGETS) + ['ignition', 'online', 'speed', 'last_online', 'remote_state', 'inboard_temp']

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--url', default='http://127.0.0.1:8123')
    ap.add_argument('--imei', required=True, help='IMEI of the tracker to observe')
    ap.add_argument('--entry-id')
    ap.add_argument('--duration', type=int, default=180)
    ap.add_argument('--interval', type=float, default=2)
    ap.add_argument('--pull', action='store_true')
    ap.add_argument('--existing-session', action='store_true')
    args = ap.parse_args()
    if not 1 <= args.duration <= 1800 or not 1 <= args.interval <= 60:
        ap.error('duration 1..1800 seconds, interval 1..60 seconds')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if args.existing_session:
        auth = json.loads(Path('/config/.storage/auth').read_text())['data']
        owners = {u['id'] for u in auth['users'] if u.get('is_owner') and u.get('is_active')}
        sessions = [t for t in auth['refresh_tokens'] if t.get('user_id') in owners and t.get('token_type') == 'normal']
        session = max(sessions, key=lambda t: t.get('last_used_at') or '')
        form = urllib.parse.urlencode({'grant_type': 'refresh_token', 'refresh_token': session['token'],
                                      'client_id': session['client_id']}).encode()
        with opener.open(urllib.request.Request(args.url+'/auth/token', data=form), timeout=10) as r:
            token = json.load(r)['access_token']
    else:
        token = Path(os.environ['HA_TOKEN_FILE']).read_text().strip()
    def request(path, data=None):
        req = urllib.request.Request(args.url+path,
            data=None if data is None else json.dumps(data).encode(),
            headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json'})
        with opener.open(req, timeout=40) as r: return json.load(r)
    if args.pull:
        services = request('/api/services')
        if not any(x['domain']=='vega' and 'read_sensors' in x['services'] for x in services):
            raise SystemExit('vega.read_sensors is not installed; no pull request sent')
    deadline = time.monotonic()+args.duration
    while time.monotonic() < deadline:
        row = {'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'states': {},
               'unverified': ['CAN sample age', 'passenger temperature zone'], 'full_profile_verified': False}
        try:
            if args.pull:
                data = {'sensor_ids':IDS, 'timeout':2}
                if args.entry_id: data['entry_id'] = args.entry_id
                row['pull'] = request('/api/services/vega/read_sensors?return_response', data)['service_response']
            states = request('/api/states')
            for s in states:
                key = s['entity_id'].split('.',1)[-1]
                prefix = 'vega_'+args.imei+'_'
                if key.startswith(prefix) and key[len(prefix):] in NAMES:
                    name = key[len(prefix):]
                    row['states'][name] = {k:s.get(k) for k in ('state','last_changed','last_updated','last_reported')}
            row['cached_value_matches'] = {
                k: row['states'].get(k,{}).get('state') in (v, v+'.0') for k,v in TARGETS.items()}
        except Exception as err:
            row['error'] = type(err).__name__
        print(json.dumps(row, ensure_ascii=False), flush=True)
        left = deadline-time.monotonic()
        if left > 0: time.sleep(min(left,args.interval))

if __name__ == '__main__': main()
