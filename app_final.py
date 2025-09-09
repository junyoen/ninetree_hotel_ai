from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import ollama
import json
import time
from datetime import datetime
import os
import uuid
import sqlite3
import threading
import atexit

HOTEL_KNOWLEDGE = {
    "basic_info": {
        "wifi_password": "Ninetree2024",
        "checkout_time": "오전 12시",
        "checkin_time": "오후 3시",
        "late_checkout": "오후 1시까지 무료 가능, 추가 요금 발생",
        "breakfast_time": "오전 7:00 - 10:00, 마지막 입장 시간 09:30",
        "breakfast_location": "2층 레스토랑",
        "pool_hours": "수영장 없음",
        "fitness_hours": "24시간",
        "parking_fee": "1박당 15,000원",
        "parking_location": "지하 1-3층"
    },
    "nearby": {
        "attractions": ["동대문 디자인 플라자", "동대문 시장", "명동"],
        "restaurants": ["광장시장 육회비빔밥", "동대문 닭한마리 골목"],
        "transport": ["지하철 4호선 동대문역 도보 3분"]
    },
    "policies": {
        "late_checkout": "오후 1시까지 무료 연장 가능, 프론트데스크 문의",
        "early_checkin": "오전 10시부터 가능, 추가 요금 발생",
        "pet_policy": "반려동물 동반 불가",
        "smoking": "전 객실 금연"
    }
}

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ninetree-hotel-secret-key-2024'
CORS(app)

# SocketIO 초기화 (실시간 채팅용)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# 데이터베이스 초기화 및 마이그레이션 (개선된 버전)
def init_database():
    """SQLite 데이터베이스 초기화 및 자동 마이그레이션"""
    conn = sqlite3.connect('hotel_chat.db')
    cursor = conn.cursor()
    
    try:
        # 1. 기본 테이블 생성 (없을 경우에만)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_number TEXT NOT NULL,
                original_message TEXT NOT NULL,
                translated_message TEXT,
                user_language TEXT NOT NULL,
                ai_response TEXT,
                requires_staff BOOLEAN DEFAULT FALSE,
                timestamp TEXT NOT NULL,
                status TEXT DEFAULT 'received'
            )
        ''')
        
        # 2. 자동 마이그레이션: 누락된 컬럼들 추가
        cursor.execute("PRAGMA table_info(messages)")
        existing_columns = [column[1] for column in cursor.fetchall()]
        
        # 필요한 컬럼들과 기본값 정의
        required_columns = {
            'priority': 'TEXT DEFAULT "medium"',
            'service_type': 'TEXT DEFAULT "general"', 
            'assigned_staff': 'TEXT'
        }
        
        # 누락된 컬럼 자동 추가
        for column_name, column_def in required_columns.items():
            if column_name not in existing_columns:
                try:
                    cursor.execute(f'ALTER TABLE messages ADD COLUMN {column_name} {column_def}')
                    print(f"✅ 컬럼 추가: {column_name}")
                except sqlite3.Error as e:
                    print(f"⚠️ 컬럼 추가 실패 ({column_name}): {e}")
        
        # 3. 채팅방 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_rooms (
                room_id TEXT PRIMARY KEY,
                room_number TEXT NOT NULL,
                language TEXT NOT NULL,
                customer_sid TEXT,
                staff_sid TEXT,
                staff_name TEXT,
                active BOOLEAN DEFAULT TRUE,
                created_at TEXT NOT NULL
            )
        ''')
        
        # 4. 채팅 메시지 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL,
                sender_type TEXT NOT NULL,
                sender_name TEXT,
                original_message TEXT NOT NULL,
                translated_message TEXT,
                language TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (room_id) REFERENCES chat_rooms (room_id)
            )
        ''')
        
        # 5. 데이터베이스 버전 테이블 (향후 마이그레이션 관리용)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS db_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        ''')
        
        # 현재 버전 설정
        cursor.execute('SELECT version FROM db_version ORDER BY version DESC LIMIT 1')
        current_version = cursor.fetchone()
        
        if not current_version:
            cursor.execute('INSERT INTO db_version (version, applied_at) VALUES (?, ?)', 
                         (1, datetime.now().isoformat()))
            print("📊 데이터베이스 버전 1.0 초기화 완료")
        
        # 6. 인덱스 생성 (성능 최적화)
        indexes = [
            'CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)',
            'CREATE INDEX IF NOT EXISTS idx_messages_room_number ON messages(room_number)',
            'CREATE INDEX IF NOT EXISTS idx_messages_requires_staff ON messages(requires_staff)',
            'CREATE INDEX IF NOT EXISTS idx_chat_rooms_active ON chat_rooms(active)'
        ]
        
        for index_sql in indexes:
            try:
                cursor.execute(index_sql)
            except sqlite3.Error as e:
                print(f"⚠️ 인덱스 생성 실패: {e}")
        
        conn.commit()
        print("🎉 데이터베이스 초기화 및 마이그레이션 완료!")
        
    except Exception as e:
        print(f"❌ 데이터베이스 초기화 오류: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

# 향후 마이그레이션 함수 (필요시 호출)
def migrate_to_version(target_version):
    """특정 버전으로 데이터베이스 마이그레이션"""
    conn = sqlite3.connect('hotel_chat.db')
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT version FROM db_version ORDER BY version DESC LIMIT 1')
        current_version = cursor.fetchone()
        current_version = current_version[0] if current_version else 0
        
        if current_version >= target_version:
            print(f"✅ 이미 버전 {target_version} 이상입니다.")
            return
        
        # 버전별 마이그레이션 로직
        migrations = {
            2: [
                # 예: 버전 2로 업그레이드시 필요한 SQL들
                "ALTER TABLE messages ADD COLUMN customer_rating INTEGER DEFAULT 5",
                "CREATE INDEX IF NOT EXISTS idx_messages_rating ON messages(customer_rating)"
            ],
            3: [
                # 예: 버전 3으로 업그레이드시 필요한 SQL들  
                "ALTER TABLE chat_rooms ADD COLUMN room_type TEXT DEFAULT 'standard'"
            ]
        }
        
        for version in range(current_version + 1, target_version + 1):
            if version in migrations:
                print(f"🔄 버전 {version}로 마이그레이션 중...")
                for sql in migrations[version]:
                    cursor.execute(sql)
                
                cursor.execute('INSERT INTO db_version (version, applied_at) VALUES (?, ?)',
                             (version, datetime.now().isoformat()))
                print(f"✅ 버전 {version} 마이그레이션 완료")
        
        conn.commit()
        print(f"🎉 데이터베이스 버전 {target_version} 업그레이드 완료!")
        
    except Exception as e:
        print(f"❌ 마이그레이션 오류: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

# Ollama 번역 및 AI 응답 클래스
class OllamaTranslator:
    def __init__(self, model='qwen2.5:7b'):
        self.model = model
        self._lock = threading.Lock()
        print(f"OllamaTranslator 초기화: 모델 {self.model}")
        
    def translate_text(self, text, source_lang, target_lang):
        """텍스트를 번역하는 함수 - 🔥 수정된 버전"""
        
        with self._lock:
            try:
                # 간단한 키워드 기반 번역 (빠른 처리)
                simple_translations = {
                    'ja': {
                        'WiFiパスワード': 'WiFi 비밀번호',
                        'Wifiパスワード': 'WiFi 비밀번호', 
                        'wifiパスワード': 'WiFi 비밀번호',
                        'ワイファイパスワード': 'WiFi 비밀번호',
                        'タオルの交換': '타월 교체',
                        'チェックアウト': '체크아웃',
                        'チェックイン': '체크인',
                        '朝食': '조식',
                        'プール': '수영장',
                        'フィットネス': '피트니스',
                        '駐車場': '주차장'
                    },
                    'en': {
                        'wifi password': 'WiFi 비밀번호',
                        'WiFi password': 'WiFi 비밀번호',
                        'wi-fi password': 'WiFi 비밀번호',
                        'towel exchange': '타월 교체',
                        'checkout time': '체크아웃 시간',
                        'checkin time': '체크인 시간',
                        'breakfast time': '조식 시간',
                        'swimming pool': '수영장',
                        'fitness center': '피트니스 센터',
                        'parking': '주차장'
                    },
                    'zh': {
                        'WiFi密码': 'WiFi 비밀번호',
                        'wifi密码': 'WiFi 비밀번호',
                        'Wifi密码': 'WiFi 비밀번호',
                        '妻子密码': 'WiFi 비밀번호',  # 잘못된 입력 교정
                        '无线密码': 'WiFi 비밀번호',
                        '网络密码': 'WiFi 비밀번호',
                        '毛巾更换': '타월 교체',
                        '退房时间': '체크아웃 시간',
                        '入住时间': '체크인 시간',
                        '早餐时间': '조식 시간',
                        '游泳池': '수영장',
                        '健身房': '피트니스',
                        '停车场': '주차장'
                    }
                }
                
                # 키워드 기반 번역 시도
                if source_lang in simple_translations:
                    for keyword, translation in simple_translations[source_lang].items():
                        if keyword in text:
                            return {
                                'success': True,
                                'translated_text': translation,
                                'translation_time': 0.1,
                                'model_used': 'keyword_based'
                            }
                
                # Ollama를 사용한 번역 - 🔥 개선된 프롬프트
                lang_names = {
                    'ko': '한국어',
                    'en': 'English', 
                    'ja': '日本語',
                    'zh': '中文'
                }
                
                source_name = lang_names.get(source_lang, source_lang)
                target_name = lang_names.get(target_lang, target_lang)
                
                # 🔥 모든 번역 케이스를 명시적으로 처리하여 불필요한 접두사 방지
                if source_lang == 'ko' and target_lang == 'zh':
                    prompt = f"请将以下韩语翻译成中文，只输出翻译结果，不要任何解释：{text}"
                elif source_lang == 'ko' and target_lang == 'ja':
                    prompt = f"以下の韓国語を日本語に翻訳してください。翻訳結果のみを出力してください：{text}"
                elif source_lang == 'ko' and target_lang == 'en':
                    prompt = f"Translate the following Korean to English. Output only the translation result: {text}"
                elif source_lang == 'zh' and target_lang == 'ko':
                    prompt = f"请将以下中文翻译成韩语，只输出翻译结果：{text}"
                elif source_lang == 'ja' and target_lang == 'ko':
                    prompt = f"以下の日本語を韓国語に翻訳してください。翻訳結果のみ出力してください：{text}"
                elif source_lang == 'en' and target_lang == 'ko':
                    prompt = f"Translate the following English to Korean. Output only the result: {text}"
                else:
                    # 기타 케이스
                    prompt = f"Translate from {source_name} to {target_name}. Output only the translation result: {text}"

                start_time = time.time()
                
                response = ollama.chat(
                    model=self.model,
                    messages=[{
                        'role': 'user',
                        'content': prompt
                    }],
                    options={
                        'num_predict': 100,
                        'temperature': 0.1
                    }
                )
                
                end_time = time.time()
                translation_time = round(end_time - start_time, 2)
                
                translated_text = response['message']['content'].strip()
                
                # 🔥 강화된 후처리 - 불필요한 접두사/설명 제거
                unwanted_prefixes = [
                    "翻译成中文是：", "翻译结果：", "翻译为：", "中文翻译：",
                    "日本語翻訳：", "日本語に翻訳すると：", "翻訳結果：",
                    "Translation:", "English translation:", "Korean translation:",
                    "번역:", "번역 결과:", "한국어 번역:", "영어 번역:",
                    "Translation result:", "The translation is:"
                ]
                
                for prefix in unwanted_prefixes:
                    if translated_text.startswith(prefix):
                        translated_text = translated_text[len(prefix):].strip()
                        break
                
                # 후처리
                corrections = {
                    "여비의 비밀번호": "WiFi 비밀번호",
                    "부인의 비밀번호": "WiFi 비밀번호",
                    "아내의 비밀번호": "WiFi 비밀번호",
                    "wi-fi": "WiFi",
                    "와이파이": "WiFi"
                }
                
                for wrong, correct in corrections.items():
                    if wrong in translated_text:
                        translated_text = translated_text.replace(wrong, correct)
                
                # 불필요한 설명 제거
                lines = translated_text.split('\n')
                translated_text = lines[0].strip()
                
                if ':' in translated_text and len(translated_text.split(':')) == 2:
                    translated_text = translated_text.split(':')[-1].strip()
                
                translated_text = translated_text.strip('"').strip("'").strip('()').strip()
                
                if not translated_text or translated_text == text:
                    translated_text = text
                
                return {
                    'success': True,
                    'translated_text': translated_text,
                    'translation_time': translation_time,
                    'model_used': self.model
                }
                
            except Exception as e:
                print(f"번역 오류: {e}")
                return {
                    'success': False,
                    'error': str(e),
                    'translated_text': text
                }

    def get_ai_response(self, message, target_language='ko'):
        """업그레이드된 지능형 AI 응답 시스템"""
        
        with self._lock:
            try:
                # 1단계: 빠른 키워드 매칭
                quick_response = self.try_quick_keyword_response(message, target_language)
                if quick_response:
                    return quick_response
                
                # 2단계: LLM 기반 지능형 응답
                return self.generate_smart_response(message, target_language)
            
            except Exception as e:
                print(f"AI 응답 생성 오류: {e}")
                return self.get_gallback_reponse(target_language)
            
    # 3. 새로운 메서드들 추가
    def try_quick_keyword_response(self, message, target_language):
        """빠른 키워드 매칭 - 다국어 응답 수정"""

        simple_responses = {
            'ko': {
                'wifi': 'WiFi 비밀번호는 "Ninetree2024"입니다.',
                'password': 'WiFi 비밀번호는 "Ninetree2024"입니다.',
                'checkout': '체크아웃 시간은 오전 11시입니다.',
                'checkin': '체크인 시간은 오후 3시입니다.',
                '조식': '조식 시간은 오전 6:30 ~ 10:00, 2층 레스토랑입니다.',
                'breakfast': '조식 시간은 오전 6:30 ~ 10:00, 2층 레스토랑입니다.',
                '수영장': '수영장은 오전 6:00 ~ 오후 10:00 이용 가능합니다.',
                'pool': '수영장은 오전 6:00 ~ 오후 10:00 이용 가능합니다.',
                '주차': '주차장은 지하 1~3층, 1박당 15,000원입니다.',
                'parking': '주차장은 지하 1~3층, 1박당 15,000원입니다.'
            },
            'en': {
                'wifi': 'WiFi password is "Ninetree2024". Available in lobby and all rooms.',
                'password': 'WiFi password is "Ninetree2024".',
                'checkout': 'Check-out time is 11:00 AM.',
                'checkin': 'Check-in time is from 3:00 PM.',
                'breakfast': 'Breakfast: 6:30 AM - 10:00 AM, 2nd floor restaurant.',
                'pool': 'Swimming pool: 6:00 AM - 10:00 PM.',
                'parking': 'Parking: B1-B3 floors, 15,000 KRW per night.'
            },
            'ja': {
                'wifi': 'WiFiパスワードは「Ninetree2024」です。ロビーと全客室でご利用いただけます。',
                'password': 'WiFiパスワードは「Ninetree2024」です。',
                'checkout': 'チェックアウト時間は午前11時です。',
                'checkin': 'チェックイン時間は午後3時からです。',
                'breakfast': '朝食は午前6:30〜10:00、2階レストランです。',
                'pool': 'プールは午前6:00〜午後10:00までご利用いただけます。',
                'parking': '駐車場は地下1〜3階、1泊15,000ウォンです。'
            },
            'zh': {
                'wifi': 'WiFi密码是"Ninetree2024"。大堂和所有客房均可使用。',
                'password': 'WiFi密码是"Ninetree2024"。',
                'checkout': '退房时间是上午11点。',
                'checkin': '入住时间从下午3点开始。',
                'breakfast': '早餐时间为上午6:30-10:00，在2楼餐厅。',
                'pool': '游泳池开放时间为上午6:00-晚上10:00。',
                'parking': '停车场位于地下1-3层，每晚15,000韩元。'
            }
        }
        
        message_lower = message.lower()
        current_responses = simple_responses.get(target_language, simple_responses['ko'])
        
        for keyword, response in current_responses.items():
            if keyword in message_lower:
                return {
                    'success': True,
                    'ai_response': response,
                    'response_time': 0.1,
                    'model_used': 'keyword_based',
                    'requires_staff': False
                }
        
        return None

    def generate_smart_response(self, message, target_language='ko'):
        """🧠 LLM 기반 지능형 응답 - 다국어 응답 수정"""
        
        system_prompt = self.create_hotel_prompt(target_language)
        
        # 언어별 사용자 프롬프트 생성
        if target_language == 'en':
            user_prompt = f"""
Hotel guest question: {message}

Please answer according to the following criteria:
1. Provide accurate hotel information if available
2. If information is not available, guide to "Please contact the front desk for more accurate information"
3. Respond in a friendly and natural tone
4. Answer concisely within 150 characters

Answer in English:
"""
        elif target_language == 'ja':
            user_prompt = f"""
ホテルゲストの質問: {message}

以下の基準で回答してください：
1. ホテル情報があれば正確な情報を提供
2. 情報がない場合は「フロントデスクにお問い合わせください」と案内
3. 親切で自然な口調で回答
4. 150文字以内で簡潔に回答

日本語で回答:
"""
        elif target_language == 'zh':
            user_prompt = f"""
       酒店客人问题: {message}

请按以下标准回答：
1. 如有酒店信息请提供准确信息
2. 如无信息请引导"请咨询前台获得更准确的答案"
3. 以友好自然的语调回答
4. 150字以内简洁回答

用中文回答:
"""
        else: #한국어
            user_prompt = f"""
호텔 고객 질문: {message}

위 질문에 대해 다음 기준으로 답변해주세요:
1. 호텔 정보가 있으면 정확한 정보 제공
2. 정보가 없으면 "프론트데스크에 문의하시면 더 정확한 답변을 받으실 수 있습니다" 안내
3. 친절하고 자연스러운 톤으로 답변
4. 150자 이내로 간단명료하게 답변

한국어로 답변:
"""
        
        start_time = time.time()
        
        try:
            response = ollama.chat(
                model=self.model,
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt}
                ],
                options={
                    'num_predict': 200,
                    'temperature': 0.2,
                    'top_p': 0.8
                }
            )
            
            end_time = time.time()
            response_time = round(end_time - start_time, 2)
            
            ai_response = response['message']['content'].strip()
            ai_response = self.clean_ai_response(ai_response)
            requires_staff = self.should_connect_staff(message, ai_response)
            
            return {
                'success': True,
                'ai_response': ai_response,
                'response_time': response_time,
                'model_used': 'llm_smart',
                'requires_staff': requires_staff
            }
            
        except Exception as e:
            print(f"LLM 응답 생성 실패: {e}")
            return self.get_fallback_response(target_language)

    def create_hotel_prompt(self, language):
        """언어별 호텔 정보 프롬프트 생성 - 다국어 지원 강화"""
        
        info = HOTEL_KNOWLEDGE
        
        if language == 'ko':
            return f"""
당신은 나인트리 동대문 호텔의 AI 컨시어지입니다.

=== 호텔 기본 정보 ===
• WiFi 비밀번호: {info['basic_info']['wifi_password']}
• 체크아웃: {info['basic_info']['checkout_time']}
• 체크인: {info['basic_info']['checkin_time']}
• 레이트 체크아웃: {info['policies']['late_checkout']}
• 조식: {info['basic_info']['breakfast_time']}, {info['basic_info']['breakfast_location']}
• 수영장: {info['basic_info']['pool_hours']}
• 피트니스: {info['basic_info']['fitness_hours']}
• 주차: {info['basic_info']['parking_location']}, {info['basic_info']['parking_fee']}

=== 주변 정보 ===
• 관광지: {', '.join(info['nearby']['attractions'])}
• 맛집: {', '.join(info['nearby']['restaurants'])}
• 교통: {', '.join(info['nearby']['transport'])}

=== 정책 ===
• 반려동물: {info['policies']['pet_policy']}
• 흡연: {info['policies']['smoking']}

=== 서비스 응답 가이드라인 ===
• 타월, 수건, 어메니티 요청: "네, 바로 가져다 드리겠습니다. 하우스키핑 직원이 곧 방문할 예정입니다."
• 청소 요청: "네, 청소 서비스를 준비하겠습니다. 직원이 곧 방문할 예정입니다."
• 룸서비스 요청: "네, 주문을 접수했습니다. 곧 준비해서 가져다 드리겠습니다."

응답 규칙:
- 친절하고 전문적인 호텔 직원 톤으로 한국어로 답변
- 정확한 정보만 제공 (추측 금지)
- 복잡한 요청은 프론트데스크 문의 안내
- 150자 이내 간단명료한 답변
"""
        
        elif language == 'en':
            return f"""
You are the AI concierge for Ninetree Dongdaemun Hotel.

=== Hotel Information ===
• WiFi Password: {info['basic_info']['wifi_password']}
• Check-out: 11:00 AM
• Check-in: 3:00 PM
• Late Check-out: Available until 1:00 PM (additional fee)
• Breakfast: 6:30-10:00 AM, 2nd floor restaurant
• Pool: 6:00 AM - 10:00 PM
• Fitness: 24 hours
• Parking: B1-B3 floors, 15,000 KRW per night

=== Nearby ===
• Attractions: Dongdaemun Design Plaza, Dongdaemun Market, Myeongdong
• Restaurants: Gwangjang Market, Dongdaemun Dakhanmari Alley
• Transport: Dongdaemun Station (Line 4) 3-min walk

=== Policies ===
• Pets: Not allowed
• Smoking: Non-smoking rooms

=== Service Response Guidelines ===
• Towel/amenity requests: "Certainly! I'll have housekeeping bring that to you right away."
• Cleaning requests: "Of course! I'll arrange cleaning service for you immediately."
• Room service requests: "Absolutely! I'll process your order and have it delivered shortly.

Response Rules:
- Friendly and professional hotel staff tone
- Provide accurate information only (no guessing)
- For complex requests, refer to front desk
- Keep responses under 150 characters
"""
        
        elif language == 'ja':
            return f"""
あなたはナインツリー東大門ホテルのAIコンシェルジュです。

=== ホテル基本情報 ===
• WiFiパスワード: {info['basic_info']['wifi_password']}
• チェックアウト: 午前11時
• チェックイン: 午後3時
• レイトチェックアウト: 午後1時まで可能（追加料金）
• 朝食: 午前6:30-10:00、2階レストラン
• プール: 午前6:00-午後10:00
• フィットネス: 24時間
• 駐車場: 地下1-3階、1泊15,000ウォン

=== 周辺情報 ===
• 観光地: 東大門デザインプラザ、東大門市場、明洞
• グルメ: 広蔵市場、東大門タッカンマリ横丁
• 交通: 地下鉄4号線東大門駅徒歩3分

=== ポリシー ===
• ペット: 同伴不可
• 喫煙: 全室禁煙

=== サービス応答ガイドライン ===
• タオル・アメニティのご要望: "承知いたしました！すぐにハウスキーピングスタッフがお持ちいたします。"
• 清掃のご要望: "もちろんです！すぐに清掃サービスを手配いたします。"
• ルームサービスのご要望: "承知いたしました！ご注文を承り、すぐにお届けいたします。"

応答ルール:
- 親切で専門的なホテルスタッフの口調で日本語で回答
- 正確な情報のみ提供（推測禁止）
- 複雑なリクエストはフロントデスクへ案内
- 150文字以内で簡潔に回答
"""
        elif language == 'zh':
            return f"""
您是九树东大门酒店的AI礼宾员。

=== 酒店基本信息 ===
• WiFi密码: {info['basic_info']['wifi_password']}
• 退房: 上午11点
• 入住: 下午3点
• 延迟退房: 下午1点前可延长（需额外费用）
• 早餐: 上午6:30-10:00，2楼餐厅
• 游泳池: 上午6:00-晚上10:00
• 健身房: 24小时
• 停车场: 地下1-3层，每晚15,000韩元

=== 周边信息 ===
• 景点: 东大门设计广场、东大门市场、明洞
• 美食: 广藏市场、东大门鸡汤一条街
• 交通: 地铁4号线东大门站步行3分钟

=== 政策 ===
• 宠物: 不允许携带
• 吸烟: 全客房禁烟

=== 服务响应指南 ===
• 毛巾/用品需求: "当然可以！我会立即安排客房服务人员为您送去。"
• 清洁需求: "没问题！我会立即为您安排清洁服务。"
• 客房服务需求: "好的！我会处理您的订单并立即为您送达。"

回复规则:
- 以友好专业的酒店员工语调用中文回答
- 仅提供准确信息（禁止猜测）
- 复杂请求请咨询前台
- 150字以内简洁回答
"""

        #기본값 (한국어)
        return self.create_hotel_prompt('ko')

    def clean_ai_response(self, response):
        """AI 응답 정제"""
        
        prefixes_to_remove = [
            "답변:", "Answer:", "응답:", "Reply:",
            "AI:", "Assistant:", "Bot:"
        ]
        
        for prefix in prefixes_to_remove:
            if response.startswith(prefix):
                response = response[len(prefix):].strip()
        
        if len(response) > 300:
            response = response[:300] + "... 더 자세한 정보는 프론트데스크에 문의해주세요."
        
        return response

    def should_connect_staff(self, original_message, ai_response):
        """직원 연결 필요성 판단"""
        
        service_keywords = [
            '타올', 'towel', 'towels', '수건', 'タオル', '毛巾',
            '청소', 'cleaning', 'clean', '掃除', '清洁',
            '가져다', 'bring', 'deliver', '持って', '送来',
            '교체', 'replace', 'change', '交換', '更换',
            '수리', 'repair', 'fix', '修理', '维修',
            '고장', 'broken', 'not working', '故障', '坏了',
            '어메니티', 'amenity', 'amenities', 'アメニティ', '用品'
        ]
        
        complex_keywords = [
            '예약', '변경', '취소', '결제', '영수증', '환불',
            'reservation', 'change', 'cancel', 'payment', 'receipt'
        ]
        
        message_lower = original_message.lower()
        
        # 서비스 요청은 직원 연결
        if any(keyword in message_lower for keyword in service_keywords):
            return True
        
        if any(keyword in message_lower for keyword in complex_keywords):
            return True
        
        if any(word in ai_response for word in ['문의', '프론트', 'front desk', 'staff']):
            return True
        
        # AI 응답에 "가져다 드리겠습니다" 같은 표현이 있으면 직원 연결
        if any(word in ai_response for word in ['가져다', '방문할', 'bring', 'deliver', 'visit']):
            return True
        
        if any(word in ai_response for word in ['문의', '프론트', 'front desk', 'staff']):
            return True

        return False

    def get_fallback_response(self, target_language):
        """LLM 실패 시 대체 응답 - 다국어 지원"""
        
        fallback_messages = {
            'ko': "죄송합니다. 잠시 시스템에 문제가 있습니다. '직원과 채팅'을 이용해주세요.",
            'en': "Sorry, there's a temporary system issue. Please use 'Chat with Staff'.",
            'ja': "申し訳ございません。一時的にシステムに問題があります。「スタッフとチャット」をご利用ください。",
            'zh': "抱歉，系统暂时出现问题。请使用'与员工聊天'。"
        }
        
        return {
            'success': False,
            'ai_response': fallback_messages.get(target_language, fallback_messages['ko']),
            'requires_staff': True,
            'model_used': 'fallback'
        }

# 전역 번역기 인스턴스
translator = OllamaTranslator(model='qwen2.5:7b')

# 데이터베이스 초기화
init_database()

# 메모리 기반 임시 저장소 (세션 관리용)
active_connections = {}  # session_id: {room_id, user_type, ...}

# 유틸리티 함수들
def detect_language(text):
    """텍스트의 언어를 감지"""
    try:
        if any('\uac00' <= char <= '\ud7af' for char in text):
            return 'ko'
        elif any('\u3040' <= char <= '\u309f' or '\u30a0' <= char <= '\u30ff' for char in text):
            return 'ja'
        elif any('\u4e00' <= char <= '\u9fff' for char in text):
            return 'zh'
        else:
            return 'en'
    except Exception:
        return 'ko'

def get_chat_room_info(room_id):
    """채팅방 정보 조회"""
    try:
        conn = sqlite3.connect('hotel_chat.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT room_number, language, staff_name 
            FROM chat_rooms 
            WHERE room_id = ? AND active = 1
        ''', (room_id,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                'room_number': result[0],
                'language': result[1],
                'staff_name': result[2]
            }
        return None
    except Exception as e:
        print(f"채팅방 정보 조회 오류: {e}")
        return None

def get_service_priority(message):
    """서비스 요청 우선순위 판단"""
    urgent_keywords = ['급해', '빨리', '긴급', 'urgent', 'asap', '今すぐ', '急ぎ', '紧急', '快']
    low_keywords = ['천천히', '나중에', 'later', 'when convenient', '後で', '慢点']
    
    message_lower = message.lower()
    
    if any(keyword in message_lower for keyword in urgent_keywords):
        return 'high'
    elif any(keyword in message_lower for keyword in low_keywords):
        return 'low'
    else:
        return 'medium'

def get_service_type(message):
    """서비스 요청 유형 분류"""
    message_lower = message.lower()
    
    if any(keyword in message_lower for keyword in ['타올', 'towel', '청소', 'cleaning', '수건']):
        return 'housekeeping'
    elif any(keyword in message_lower for keyword in ['음식', 'food', '룸서비스', 'room service']):
        return 'room-service'
    elif any(keyword in message_lower for keyword in ['고장', '문제', 'repair', 'broken', 'problem']):
        return 'maintenance'
    else:
        return 'concierge'

# Flask 라우트들
@app.route('/')
def index():
    """메인 페이지"""
    return render_template('index.html')

@app.route('/message')
def message_page():
    """메시지 작성 페이지"""
    return render_template('message.html')

@app.route('/admin')
def admin_page():
    """관리자 페이지"""
    return render_template('admin.html')

@app.route('/staff-chat')
def staff_chat_page():
    """직원용 채팅 페이지"""
    return render_template('staff_chat.html')

# SocketIO 이벤트 핸들러들
@socketio.on('connect')
def handle_connect():
    """클라이언트 연결"""
    try:
        print(f'✅ 클라이언트 연결: {request.sid}')
        active_connections[request.sid] = {
            'connected_at': datetime.now().isoformat(),
            'room_id': None,
            'user_type': None
        }
    except Exception as e:
        print(f'❌ 연결 처리 오류: {e}')

@socketio.on('disconnect')
def handle_disconnect():
    """클라이언트 연결 해제"""
    try:
        print(f'❌ 클라이언트 연결 해제: {request.sid}')
        
        if request.sid in active_connections:
            connection_info = active_connections[request.sid]
            room_id = connection_info.get('room_id')
            
            if room_id:
                leave_room(room_id)
                socketio.emit('user_left', {
                    'message': '상대방이 채팅을 종료했습니다.',
                    'user_type': connection_info.get('user_type')
                }, room=room_id)
            
            del active_connections[request.sid]
        
    except Exception as e:
        print(f'❌ 연결 해제 처리 오류: {e}')

@socketio.on('join_room')
def handle_join_room(data):
    """채팅방 참가"""
    try:
        room_id = data.get('room_id')
        user_type = data.get('user_type', 'customer')
        
        if not room_id:
            emit('error', {'message': '채팅방 ID가 필요합니다.'})
            return
        
        join_room(room_id)
        
        if request.sid in active_connections:
            active_connections[request.sid]['room_id'] = room_id
            active_connections[request.sid]['user_type'] = user_type
        
        if user_type == 'staff':
            staff_name = data.get('staff_name', '직원')
            
            # 데이터베이스 업데이트
            conn = sqlite3.connect('hotel_chat.db')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE chat_rooms 
                SET staff_sid = ?, staff_name = ? 
                WHERE room_id = ?
            ''', (request.sid, staff_name, room_id))
            conn.commit()
            conn.close()
            
            # 고객에게 직원 참여 알림
            socketio.emit('staff_joined', {
                'staff_name': staff_name,
                'message': f'{staff_name} 직원이 채팅에 참여했습니다.'
            }, room=room_id)
            
            print(f'👨‍💼 직원 {staff_name}이 채팅방 {room_id}에 참가')
        
        else:  # customer
            print(f'👤 고객이 채팅방 {room_id}에 참가')
        
    except Exception as e:
        print(f'❌ 채팅방 참가 오류: {e}')
        emit('error', {'message': f'채팅방 참가 실패: {str(e)}'})

@socketio.on('leave_room')
def handle_leave_room(data):
    """채팅방 나가기"""
    try:
        room_id = data.get('room_id')
        if room_id:
            leave_room(room_id)
            
            if request.sid in active_connections:
                active_connections[request.sid]['room_id'] = None
            
            print(f'🚪 {request.sid}이 채팅방 {room_id}에서 나감')
        
    except Exception as e:
        print(f'❌ 채팅방 나가기 오류: {e}')

@socketio.on('send_message')
def handle_send_message(data):
    """메시지 전송 (핵심 수정 부분)"""
    try:
        room_id = data.get('room_id')
        message = data.get('message', '').strip()
        sender_type = data.get('sender_type', 'customer')
        sender_name = data.get('sender_name', '고객' if sender_type == 'customer' else '직원')
        message_language = data.get('language', 'ko')
        
        if not room_id or not message:
            emit('error', {'message': '방 ID와 메시지가 필요합니다.'})
            return
        
        # 메시지 데이터 생성
        message_data = {
            'id': str(uuid.uuid4()),
            'room_id': room_id,
            'sender_type': sender_type,
            'sender_name': sender_name,
            'original_message': message,
            'language': message_language,
            'timestamp': datetime.now().isoformat(),
            'translated_message': None
        }
        
        # 번역 처리
        if sender_type == 'customer' and message_language != 'ko':
            # 고객 메시지를 한국어로 번역 (직원용)
            translation_result = translator.translate_text(message, message_language, 'ko')
            if translation_result['success']:
                message_data['translated_message'] = translation_result['translated_text']
        elif sender_type == 'staff':
            # 직원 메시지를 고객 언어로 번역
            chat_info = get_chat_room_info(room_id)
            if chat_info and chat_info['language'] != 'ko':
                translation_result = translator.translate_text(message, 'ko', chat_info['language'])
                if translation_result['success']:
                    message_data['translated_message'] = translation_result['translated_text']
        
        # 데이터베이스에 저장
        conn = sqlite3.connect('hotel_chat.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO chat_messages 
            (room_id, sender_type, sender_name, original_message, translated_message, language, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (room_id, sender_type, sender_name, message, 
              message_data['translated_message'], message_language, message_data['timestamp']))
        conn.commit()
        conn.close()
        
        # 🔥 핵심 수정: 해당 채팅방의 모든 참가자에게 메시지 전송 (include_self=True 중요!)
        socketio.emit('receive_message', message_data, room=room_id, include_self=True)
        
        print(f'💬 메시지 전송 성공 - 방: {room_id}, 보낸이: {sender_name} ({sender_type}), 내용: {message[:30]}...')
        
    except Exception as e:
        print(f'❌ 메시지 전송 오류: {e}')
        emit('error', {'message': f'메시지 전송 실패: {str(e)}'})

@socketio.on('request_staff')
def handle_request_staff(data):
    """직원 연결 요청"""
    try:
        room_number = data.get('room_number')
        customer_language = data.get('language', 'ko')
        
        if not room_number:
            emit('error', {'message': '객실 번호가 필요합니다.'})
            return
        
        # 채팅방 ID 생성
        room_id = f"chat_{room_number}_{int(time.time())}"
        
        # 데이터베이스에 채팅방 저장
        conn = sqlite3.connect('hotel_chat.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO chat_rooms 
            (room_id, room_number, language, customer_sid, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (room_id, room_number, customer_language, request.sid, True, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        
        # 고객을 채팅방에 입장시킴
        join_room(room_id)
        
        # 연결 정보 업데이트
        if request.sid in active_connections:
            active_connections[request.sid]['room_id'] = room_id
            active_connections[request.sid]['user_type'] = 'customer'
        
        # 직원들에게 연결 요청 브로드캐스트
        socketio.emit('staff_request', {
            'room_id': room_id,
            'room_number': room_number,
            'language': customer_language,
            'customer_id': request.sid
        })
        
        # 고객에게 대기 메시지 전송
        wait_messages = {
            'ko': '직원 연결을 요청했습니다. 잠시만 기다려주세요.',
            'en': 'Staff connection requested. Please wait a moment.',
            'ja': 'スタッフ接続を要求しました。少々お待ちください。',
            'zh': '已请求员工连接。请稍等。'
        }
        
        emit('system_message', {
            'message': wait_messages.get(customer_language, wait_messages['ko']),
            'type': 'info',
            'room_id': room_id
        })
        
        print(f'📞 직원 연결 요청: 객실 {room_number} ({customer_language}) -> 채팅방 {room_id}')
        
    except Exception as e:
        print(f'❌ 직원 연결 요청 오류: {e}')
        emit('error', {'message': f'직원 연결 요청 실패: {str(e)}'})

# REST API 엔드포인트들
@app.route('/api/translate', methods=['POST'])
def translate_api():
    """번역 API"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'JSON 데이터가 필요합니다.'}), 400
            
        text = data.get('text', '')
        source_lang = data.get('source_lang', 'auto')
        target_lang = data.get('target_lang', 'ko')
        
        if not text:
            return jsonify({'error': '번역할 텍스트가 없습니다.'}), 400
        
        # 언어 자동 감지
        if source_lang == 'auto':
            source_lang = detect_language(text)
        
        result = translator.translate_text(text, source_lang, target_lang)
        return jsonify(result)
        
    except Exception as e:
        print(f'❌ 번역 API 오류: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/ai-response', methods=['POST'])
def ai_response_api():
    """AI 자동 응답 API"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'JSON 데이터가 필요합니다.'}), 400
            
        message = data.get('message', '')
        target_language = data.get('target_language', 'ko')
        
        if not message:
            return jsonify({'error': '메시지가 없습니다.'}), 400
        
        result = translator.get_ai_response(message, target_language)
        return jsonify(result)
        
    except Exception as e:
        print(f'❌ AI 응답 API 오류: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/send-message', methods=['POST'])
def send_message_api():
    """메시지 전송 API (AI 응답 포함)"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'JSON 데이터가 필요합니다.'}), 400
            
        room_number = data.get('room_number', '')
        original_message = data.get('original_message', '')
        user_language = data.get('user_language', 'ko')
        timestamp = data.get('timestamp', datetime.now().isoformat())
        
        if not room_number or not original_message:
            return jsonify({'error': '객실 번호와 메시지가 필요합니다.'}), 400
        
        # 언어 자동 감지
        detected_language = detect_language(original_message)
        if detected_language != user_language:
            user_language = detected_language
        
        # 1. 메시지 번역 (한국어가 아닐 경우)
        translated_message = original_message
        if user_language != 'ko':
            translation_result = translator.translate_text(original_message, user_language, 'ko')
            if translation_result['success']:
                translated_message = translation_result['translated_text']
        
        # 2. AI 응답 생성 (사용자 언어로)
        ai_result = translator.get_ai_response(translated_message, user_language)
        ai_response = None
        requires_staff = False
        
        if ai_result['success']:
            ai_response = ai_result['ai_response']
            requires_staff = ai_result.get('requires_staff', False)
        
        # 3. 서비스 우선순위 및 유형 판단
        priority = get_service_priority(translated_message) if requires_staff else 'low'
        service_type = get_service_type(translated_message) if requires_staff else 'general'
        
        # 4. 데이터베이스에 메시지 저장
        conn = sqlite3.connect('hotel_chat.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO messages 
            (room_number, original_message, translated_message, user_language, ai_response, 
             requires_staff, timestamp, status, priority, service_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (room_number, original_message, translated_message, user_language, ai_response, 
              requires_staff, timestamp, 
              'ai_responded' if ai_response and not requires_staff else 'needs_staff',
              priority, service_type))
        
        message_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        print(f"📩 새 메시지 저장: 객실 {room_number} ({user_language}) - {original_message[:30]}...")
        print(f"🤖 AI 응답: {ai_response[:30] if ai_response else 'None'}...")
        print(f"👨‍💼 직원 필요: {requires_staff}, 우선순위: {priority}, 유형: {service_type}")
        
        return jsonify({
            'success': True,
            'message_id': message_id,
            'ai_response': ai_response,
            'requires_staff': requires_staff,
            'priority': priority,
            'service_type': service_type
        })
        
    except Exception as e:
        print(f"❌ 메시지 전송 API 오류: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/messages', methods=['GET'])
def get_messages_api():
    """관리자용 메시지 목록 API"""
    try:
        conn = sqlite3.connect('hotel_chat.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, room_number, original_message, translated_message, user_language, 
                   ai_response, requires_staff, timestamp, status, priority, service_type, assigned_staff
            FROM messages 
            ORDER BY timestamp DESC
            LIMIT 100
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        messages = []
        for row in rows:
            messages.append({
                'id': row[0],
                'room_number': row[1],
                'original_message': row[2],
                'translated_message': row[3],
                'user_language': row[4],
                'ai_response': row[5],
                'requires_staff': bool(row[6]),
                'timestamp': row[7],
                'status': row[8],
                'priority': row[9],
                'service_type': row[10],
                'assigned_staff': row[11]
            })
        
        return jsonify({
            'success': True,
            'messages': messages
        })
        
    except Exception as e:
        print(f'❌ 메시지 목록 API 오류: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/service-requests', methods=['GET'])
def get_service_requests_api():
    """서비스 요청 목록 API"""
    try:
        conn = sqlite3.connect('hotel_chat.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, room_number, original_message, translated_message, user_language, 
                   timestamp, status, priority, service_type, assigned_staff
            FROM messages 
            WHERE requires_staff = 1
            ORDER BY 
                CASE priority
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                END, timestamp DESC
            LIMIT 100
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        service_requests = []
        for row in rows:
            service_requests.append({
                'id': row[0],
                'room_number': row[1],
                'original_message': row[2],
                'translated_message': row[3],
                'user_language': row[4],
                'timestamp': row[5],
                'status': row[6],
                'priority': row[7],
                'service_type': row[8],
                'assigned_staff': row[9]
            })
        
        return jsonify({
            'success': True,
            'service_requests': service_requests
        })
        
    except Exception as e:
        print(f'❌ 서비스 요청 API 오류: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/service-requests/<int:request_id>/status', methods=['PUT'])
def update_service_status_api(request_id):
    """서비스 요청 상태 업데이트 API"""
    try:
        data = request.get_json()
        new_status = data.get('status', 'completed')
        assigned_staff = data.get('assigned_staff', '')
        
        conn = sqlite3.connect('hotel_chat.db')
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE messages 
            SET status = ?, assigned_staff = ?
            WHERE id = ?
        ''', (new_status, assigned_staff, request_id))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
        
    except Exception as e:
        print(f'❌ 서비스 상태 업데이트 오류: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/chat-rooms', methods=['GET'])
def get_chat_rooms_api():
    """활성 채팅방 목록 API"""
    try:
        conn = sqlite3.connect('hotel_chat.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT room_id, room_number, language, customer_sid, staff_sid, staff_name, active, created_at
            FROM chat_rooms 
            WHERE active = 1
            ORDER BY created_at DESC
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        chat_rooms = []
        for row in rows:
            chat_rooms.append({
                'room_id': row[0],
                'room_number': row[1],
                'language': row[2],
                'customer_sid': row[3],
                'staff_sid': row[4],
                'staff_name': row[5],
                'active': bool(row[6]),
                'created_at': row[7]
            })
        
        # 활성 직원 정보
        active_staff = {}
        for sid, info in active_connections.items():
            if info.get('user_type') == 'staff':
                active_staff[sid] = info
        
        return jsonify({
            'success': True,
            'chat_rooms': chat_rooms,
            'active_staff': active_staff,
            'total_connections': len(active_connections)
        })
        
    except Exception as e:
        print(f'❌ 채팅방 목록 API 오류: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """서버 상태 확인"""
    try:
        # Ollama 연결 테스트
        models = ollama.list()
        
        # 데이터베이스 연결 테스트
        conn = sqlite3.connect('hotel_chat.db')
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM messages')
        message_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM chat_rooms WHERE active = 1')
        active_room_count = cursor.fetchone()[0]
        conn.close()
        
        return jsonify({
            'status': 'healthy',
            'ollama_connected': True,
            'available_models': [model.model for model in models.models],
            'current_model': translator.model,
            'total_messages': message_count,
            'active_chat_rooms': active_room_count,
            'active_connections': len(active_connections),
            'database_connected': True
        })
        
    except Exception as e:
        print(f'❌ 헬스 체크 오류: {e}')
        return jsonify({
            'status': 'unhealthy',
            'ollama_connected': False,
            'database_connected': False,
            'error': str(e)
        }), 500

# 에러 핸들러
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Page not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500

# 정리 함수 (서버 종료시 실행)
def cleanup_on_exit():
    """서버 종료시 정리 작업"""
    try:
        # 모든 채팅방을 비활성화
        conn = sqlite3.connect('hotel_chat.db')
        cursor = conn.cursor()
        cursor.execute('UPDATE chat_rooms SET active = 0')
        conn.commit()
        conn.close()
        print('🧹 서버 종료시 정리 작업 완료')
    except Exception as e:
        print(f'❌ 정리 작업 오류: {e}')

atexit.register(cleanup_on_exit)

# 메인 실행 부분
if __name__ == '__main__':
    # 필요한 폴더 생성
    if not os.path.exists('templates'):
        os.makedirs('templates')
    if not os.path.exists('static'):
        os.makedirs('static')
    
    print("🚀 나인트리 호텔 실시간 채팅 시스템 시작!")
    print(f"🤖 사용 중인 AI 모델: {translator.model}")
    print("📱 고객용 페이지: http://localhost:5000")
    print("🔧 관리자 페이지: http://localhost:5000/admin")
    print("👨‍💼 직원 채팅: http://localhost:5000/staff-chat")
    print("💻 API 상태 확인: http://localhost:5000/api/health")
    print("💾 데이터베이스: hotel_chat.db (SQLite)")
    
    # SocketIO 서버 실행
    import os
    port = int(os.environ.get('PORT', 5000))
    socketio.run(
        app, 
        debug=False, 
        host='0.0.0.0', 
        port=5000, 
        allow_unsafe_werkzeug=True
    )