from flask import Flask, request, jsonify, send_from_directory
import requests
import os
import traceback

app = Flask(__name__, static_folder='static')

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
NEWS_API_KEY = os.environ.get('NEWS_API_KEY', 'babbb951d220490a81cccfd354d348c2')

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/signal', methods=['POST', 'OPTIONS'])
def get_signal():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        data = request.json
        prompt = data.get('prompt', '')
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01'
            },
            json={
                'model': 'claude-haiku-4-5-20251001',
                'max_tokens': 1000,
                'messages': [{'role': 'user', 'content': prompt}]
            },
            timeout=30
        )
        if response.status_code != 200:
            error_body = response.text
            print(f"Anthropic API error {response.status_code}: {error_body}")
            return jsonify({'error': f'API error {response.status_code}: {error_body}'}), 500
        result = response.json()
        text = ''.join([b.get('text', '') for b in result.get('content', [])])
        return jsonify({'text': text})
    except Exception as e:
        print(f"Exception: {traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/news', methods=['GET', 'OPTIONS'])
def get_news():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        q = 'gold price OR "XAU/USD" OR "gold market" OR "Federal Reserve" OR inflation'
        url = f'https://newsapi.org/v2/everything?q={requests.utils.quote(q)}&language=en&sortBy=publishedAt&pageSize=6&apiKey={NEWS_API_KEY}'
        r = requests.get(url, timeout=10)
        if not r.ok:
            return jsonify({'articles': [], 'error': f'NewsAPI {r.status_code}'}), 200
        d = r.json()
        articles = d.get('articles', [])
        # Return only what we need
        clean = [{'headline': a.get('title',''), 'source': a.get('source',{}).get('name',''), 'publishedAt': a.get('publishedAt','')} for a in articles]
        return jsonify({'articles': clean})
    except Exception as e:
        print(f"News error: {traceback.format_exc()}")
        return jsonify({'articles': [], 'error': str(e)}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'key_set': bool(ANTHROPIC_API_KEY)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
