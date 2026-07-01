from flask import Flask, request, jsonify, send_from_directory
import requests
import os
import traceback
import threading
import time
import json
from datetime import datetime

app = Flask(__name__, static_folder='static')

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
NEWS_API_KEY = os.environ.get('NEWS_API_KEY', 'babbb951d220490a81cccfd354d348c2')
TG_TOKEN = os.environ.get('TG_TOKEN', '8947905331:AAGq8NINPfkVHgpQU2muN8G690qMhm0xR6M')
TG_CHAT = os.environ.get('TG_CHAT', '1673781813')

# ── Alpaca Paper Trading ──────────────────────────────────────────────────────
ALPACA_KEY    = os.environ.get('ALPACA_KEY', '')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET', '')
ALPACA_BASE   = 'https://paper-api.alpaca.markets/v2'
# ─────────────────────────────────────────────────────────────────────────────

last_signal = None

def send_telegram(msg):
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def fetch_gold_price():
    try:
        r = requests.get('https://api.gold-api.com/price/XAU', timeout=10)
        d = r.json()
        return {'price': d['price'], 'open': d.get('prev_close_price', d['price']*0.999)}
    except:
        return {'price': 4000.0, 'open': 3996.0, 'sim': True}

def fetch_dxy():
    try:
        r = requests.get('https://api.gold-api.com/price/DXY', timeout=10)
        d = r.json()
        return {'value': d['price'], 'change': d['price'] - d.get('prev_close_price', d['price'])}
    except:
        return {'value': 104.2, 'change': 0.02}

def fetch_news():
    try:
        q = 'gold price OR "XAU/USD" OR "Federal Reserve" OR inflation'
        url = f'https://newsapi.org/v2/everything?q={requests.utils.quote(q)}&language=en&sortBy=publishedAt&pageSize=4&apiKey={NEWS_API_KEY}'
        r = requests.get(url, timeout=10)
        d = r.json()
        return [a.get('title','') for a in d.get('articles', [])[:4]]
    except:
        return []

def analyze_signal(price, dxy, news):
    news_text = '\n'.join([f'{i+1}. {n}' for i, n in enumerate(news)]) if news else 'Sin noticias'
    prompt = f"""Eres analista experto en XAU/USD. Analiza y genera señal de trading.

PRECIO: ${price['price']:.2f} | Apertura: ${price['open']:.2f} | Cambio: {((price['price']-price['open'])/price['open']*100):.2f}%
DXY: {dxy['value']:.2f} ({'+' if dxy['change']>=0 else ''}{dxy['change']:.2f})

NOTICIAS:
{news_text}

CONTEXTO: Oro en zona $3800-4200, Fed con tasas 4.25-4.5%, bancos centrales comprando oro.

Responde SOLO JSON sin backticks:
{{"signal":"COMPRAR o VENDER o ESPERAR","confidence":número 40-93,"entry":número,"takeProfit":número,"stopLoss":número,"rrRatio":número,"reasoning":"2 frases en español","timeframe":"intradía o corto plazo"}}"""

    r = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01'},
        json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 500, 'messages': [{'role': 'user', 'content': prompt}]},
        timeout=30
    )
    if r.status_code != 200:
        raise Exception(f'API error {r.status_code}')
    text = ''.join([b.get('text','') for b in r.json().get('content',[])])
    text = text.strip().replace('```json','').replace('```','').strip()
    import re
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        raise Exception('No JSON in response')
    return json.loads(match.group(0))

# ── Alpaca: ejecutar orden ────────────────────────────────────────────────────
def execute_alpaca_order(signal, confidence, sl_price, tp_price):
    """Ejecuta orden en Alpaca Paper Trading usando GLD (ETF oro ~1:1 con XAU)"""
    if not ALPACA_KEY or not ALPACA_SECRET:
        print("[Alpaca] Credenciales no configuradas — saltando orden")
        return None

    if confidence < 65:
        print(f"[Alpaca] Confianza {confidence}% < 65% — orden no ejecutada")
        return None

    headers = {
        'APCA-API-KEY-ID': ALPACA_KEY,
        'APCA-API-SECRET-KEY': ALPACA_SECRET,
        'Content-Type': 'application/json'
    }

    try:
        # Verificar cuenta
        account_resp = requests.get(f'{ALPACA_BASE}/account', headers=headers, timeout=10)
        account = account_resp.json()
        equity = float(account.get('equity', 0))

        if equity <= 0:
            print("[Alpaca] Sin fondos disponibles")
            return None

        # Obtener precio GLD para calcular unidades
        quote_resp = requests.get(
            f'{ALPACA_BASE}/stocks/GLD/quotes/latest',
            headers=headers,
            timeout=10
        )
        quote_data = quote_resp.json()
        gld_price = float(quote_data.get('quote', {}).get('ap', 200))
        if gld_price <= 0:
            gld_price = 200.0  # fallback

        # Riesgo 1% del capital
        risk_amount = equity * 0.01
        qty = max(1, int(risk_amount / gld_price))

        side = 'buy' if signal == 'COMPRAR' else 'sell'

        order_data = {
            'symbol': 'GLD',
            'qty': str(qty),
            'side': side,
            'type': 'market',
            'time_in_force': 'day'
        }

        order_resp = requests.post(
            f'{ALPACA_BASE}/orders',
            headers=headers,
            json=order_data,
            timeout=10
        )
        order = order_resp.json()
        order_id = order.get('id', 'N/A')
        order_status = order.get('status', 'unknown')

        emoji = '🟢' if side == 'buy' else '🔴'
        msg = (
            f"{emoji} *ORDEN ALPACA EJECUTADA*\n\n"
            f"📌 *Acción:* {'COMPRA' if side == 'buy' else 'VENTA'} GLD\n"
            f"📦 *Cantidad:* {qty} acciones\n"
            f"📊 *Confianza IA:* {confidence}%\n"
            f"💰 *Capital cuenta:* ${equity:,.2f}\n"
            f"🆔 *ID Orden:* `{order_id}`\n"
            f"✅ *Estado:* {order_status}\n\n"
            f"_GLD = ETF que sigue el precio del oro_"
        )
        send_telegram(msg)
        print(f"[Alpaca] Orden ejecutada: {order_id} ({order_status})")
        return order_id

    except Exception as e:
        error_msg = f"❌ *Error Alpaca*: {str(e)}"
        print(f"[Alpaca] Error: {traceback.format_exc()}")
        send_telegram(error_msg)
        return None
# ─────────────────────────────────────────────────────────────────────────────

def auto_analysis():
    global last_signal
    print(f"[{datetime.now()}] Running auto analysis...")
    try:
        price = fetch_gold_price()
        dxy = fetch_dxy()
        news = fetch_news()
        sig = analyze_signal(price, dxy, news)

        confidence = sig.get('confidence', 0)
        signal_type = sig.get('signal', 'ESPERAR')

        print(f"Signal: {signal_type} ({confidence}%) | Last: {last_signal}")

        # Send alert if signal changed and confidence >= 65
        if signal_type != 'ESPERAR' and confidence >= 65 and signal_type != last_signal:
            emoji = '🟢' if signal_type == 'COMPRAR' else '🔴'
            msg = f"""{emoji} *AURUM v2 · XAU/USD*

📌 *Acción:* {signal_type}
💰 *Precio:* ${price['price']:.2f}
🎯 *Entrada:* ${sig.get('entry', 0):.2f}
✅ *TP:* ${sig.get('takeProfit', 0):.2f}
🛑 *SL:* ${sig.get('stopLoss', 0):.2f}
⚖️ *R:R:* {sig.get('rrRatio', 0):.1f}:1
📊 *Confianza:* {confidence}%
⏱ *Horizonte:* {sig.get('timeframe', 'intradía')}

💬 _{sig.get('reasoning', '')}_

_⚠️ No es asesoramiento financiero._"""
            send_telegram(msg)
            last_signal = signal_type
            print(f"Alert sent: {signal_type}")

            # ── Ejecutar orden en Alpaca ──────────────────────────────────
            execute_alpaca_order(
                signal=signal_type,
                confidence=confidence,
                sl_price=sig.get('stopLoss', 0),
                tp_price=sig.get('takeProfit', 0)
            )
            # ─────────────────────────────────────────────────────────────

        elif signal_type == 'ESPERAR':
            last_signal = None  # Reset so next real signal triggers alert

    except Exception as e:
        print(f"Auto analysis error: {traceback.format_exc()}")

def scheduler():
    # Wait 2 min after startup then run every 15 min
    time.sleep(120)
    while True:
        auto_analysis()
        time.sleep(900)  # 15 minutes

# Start scheduler in background thread
scheduler_thread = threading.Thread(target=scheduler, daemon=True)
scheduler_thread.start()
print("Auto-scheduler started (every 15 min)")

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
            headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01'},
            json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 1000, 'messages': [{'role': 'user', 'content': prompt}]},
            timeout=30
        )
        if response.status_code != 200:
            print(f"Anthropic error {response.status_code}: {response.text}")
            return jsonify({'error': f'API error {response.status_code}: {response.text}'}), 500
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
        clean = [{'headline': a.get('title',''), 'source': a.get('source',{}).get('name',''), 'publishedAt': a.get('publishedAt','')} for a in articles]
        return jsonify({'articles': clean})
    except Exception as e:
        print(f"News error: {traceback.format_exc()}")
        return jsonify({'articles': [], 'error': str(e)}), 200

@app.route('/api/status', methods=['GET'])
def status():
    return jsonify({
        'status': 'ok',
        'scheduler': 'running',
        'last_signal': last_signal,
        'alpaca_configured': bool(ALPACA_KEY and ALPACA_SECRET),
        'time': datetime.now().isoformat()
    })

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'key_set': bool(ANTHROPIC_API_KEY)})

# ── Alpaca: estado de cuenta ──────────────────────────────────────────────────
@app.route('/api/alpaca/status', methods=['GET'])
def alpaca_status():
    if not ALPACA_KEY or not ALPACA_SECRET:
        return jsonify({'error': 'Alpaca no configurado — añade ALPACA_KEY y ALPACA_SECRET en Railway'})
    headers = {
        'APCA-API-KEY-ID': ALPACA_KEY,
        'APCA-API-SECRET-KEY': ALPACA_SECRET
    }
    try:
        account  = requests.get(f'{ALPACA_BASE}/account',                          headers=headers, timeout=10).json()
        positions= requests.get(f'{ALPACA_BASE}/positions',                        headers=headers, timeout=10).json()
        orders   = requests.get(f'{ALPACA_BASE}/orders?status=all&limit=10',       headers=headers, timeout=10).json()
        return jsonify({
            'equity':        account.get('equity'),
            'cash':          account.get('cash'),
            'buying_power':  account.get('buying_power'),
            'positions':     positions,
            'recent_orders': orders
        })
    except Exception as e:
        return jsonify({'error': str(e)})
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
