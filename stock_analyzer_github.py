import warnings
warnings.filterwarnings('ignore')

import FinanceDataReader as fdr
import requests
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
import datetime
import time
import json
from pykrx import stock as krx_stock

# ==========================================
# ⚙️ 설정 정보
# ==========================================
import os
   
   KAKAO_API_KEY = os.getenv('KAKAO_API_KEY', 'e05f3ab91ac650d6fbe6bec96d20d4af')
   KAKAO_REFRESH_TOKEN = os.getenv('KAKAO_REFRESH_TOKEN', 'ilI_ybunWidhf6sRNACYk1nwSR7EKI_lAAAAAgoXACcAAAGcK_Dm4x7SOb8w2j0_')
   KAKAO_REDIRECT_URI = os.getenv('KAKAO_REDIRECT_URI', 'https://localhost:5000')

# ==========================================
# 🔧 가장 최근 영업일 계산
# ==========================================
def get_last_business_day():
    now = datetime.datetime.now()
    target = now

    if now.weekday() == 5:
        target = now - datetime.timedelta(days=1)
        reason = "토요일 → 금요일 데이터 사용"
    elif now.weekday() == 6:
        target = now - datetime.timedelta(days=2)
        reason = "일요일 → 금요일 데이터 사용"
    elif now.weekday() == 0 and now.hour < 16:
        target = now - datetime.timedelta(days=3)
        reason = "월요일 오전 → 금요일 데이터 사용"
    elif now.hour < 16:
        target = now - datetime.timedelta(days=1)
        reason = "장 마감 전 → 전날 데이터 사용"
    else:
        reason = "장 마감 후 → 당일 데이터 사용"

    return target, reason

# ==========================================
# 🔧 핵심 기능 1: 네이버 금융에서 수급 크롤링
# ==========================================
def get_investor_data_naver(code, debug=False):
    try:
        url = f'https://finance.naver.com/item/frgn.naver?code={code}'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        
        soup = BeautifulSoup(res.text, 'html.parser')
        tables = soup.select('table.type2')
        
        if len(tables) < 2:
            if debug:
                print(f"      ⚠️ 수급 테이블 없음")
            return 0, 0

        table = tables[1]
        rows = table.select('tr')

        for row in rows:
            cols = row.select('td')
            if len(cols) < 7:
                continue
            date_text = cols[0].text.strip()
            if '.' not in date_text or len(date_text) < 8:
                continue
            try:
                inst_text = cols[5].text.strip().replace(',', '').replace('+', '')
                frgn_text = cols[6].text.strip().replace(',', '').replace('+', '')
                inst_vol = int(inst_text) if inst_text.lstrip('-').isdigit() else 0
                frgn_vol = int(frgn_text) if frgn_text.lstrip('-').isdigit() else 0
                if debug:
                    print(f"      📊 {date_text}: 기관 {inst_vol:,}주 / 외국인 {frgn_vol:,}주")
                return inst_vol, frgn_vol
            except (ValueError, IndexError) as e:
                if debug:
                    print(f"      ⚠️ 파싱 오류: {e}")
                continue

        return 0, 0

    except requests.exceptions.Timeout:
        if debug:
            print(f"      ⚠️ 타임아웃 (10초 초과)")
        return 0, 0
    except Exception as e:
        if debug:
            print(f"      ❌ 크롤링 오류: {e}")
        return 0, 0

# ==========================================
# 🔧 핵심 기능 2: 변동성 계산
# ==========================================
def calculate_volatility(df):
    if df is None or len(df) < 20:
        return "보통", 0.07, 0.04

    df_copy = df.copy()
    df_copy['Returns'] = df_copy['Close'].pct_change()
    volatility = df_copy['Returns'].tail(20).std() * np.sqrt(20) * 100

    if volatility >= 10:
        return "고변동", 0.10, 0.05
    elif volatility <= 5:
        return "저변동", 0.05, 0.03
    else:
        return "보통", 0.07, 0.04

# ==========================================
# 🔧 핵심 기능 3: 이동평균선 정배열 체크
# ==========================================
def check_ma_alignment(df):
    if df is None or len(df) < 120:
        return False, 0, 0, 0, 0

    try:
        close = df['Close'].iloc[-1]
        ma20 = df['Close'].tail(20).mean()
        ma60 = df['Close'].tail(60).mean()
        ma120 = df['Close'].tail(120).mean()

        score = 0
        if close > ma20:
            score += 1
        if close > ma60:
            score += 1
        if close > ma120:
            score += 1
        if ma20 > ma60 > ma120:
            score += 1

        is_aligned = (close > ma120)

        return is_aligned, int(ma20), int(ma60), int(ma120), score
    except Exception as e:
        print(f"      ⚠️ 이동평균 계산 오류: {e}")
        return False, 0, 0, 0, 0

# ==========================================
# 🔧 핵심 기능 4: 지지구간 분석
# ==========================================
def find_support_level(df):
    if df is None or len(df) < 20:
        return 0, "약함", "데이터 부족"

    try:
        close = df['Close'].iloc[-1]
        recent_20 = df.tail(20)

        low_20 = recent_20['Low'].min()
        avg_low = recent_20['Low'].mean()
        ma20 = df['Close'].tail(20).mean()

        support_candidates = [low_20, avg_low, ma20]
        valid_supports = [s for s in support_candidates if s < close]

        if not valid_supports:
            return int(close * 0.95), "약함", "명확한 지지선 없음"

        support = max(valid_supports)
        touch_count = sum(1 for low in recent_20['Low'] if abs(low - support) / support < 0.02)

        if touch_count >= 3:
            strength = "강함"
            desc = f"최근 {touch_count}회 지지 확인"
        elif touch_count >= 2:
            strength = "보통"
            desc = f"최근 {touch_count}회 지지 테스트"
        else:
            strength = "약함"
            desc = "단기 저점 기준"

        distance = (close - support) / close * 100
        return int(support), strength, f"{desc} (현재가 대비 -{distance:.1f}%)"
    except Exception as e:
        print(f"      ⚠️ 지지선 계산 오류: {e}")
        return 0, "약함", "계산 실패"

# ==========================================
# 🔧 핵심 기능 5: 재무 안전성 필터 (중장기 투자용)
# ==========================================
def get_financial_fundamentals(code, target_date):
    """
    PyKrx로 PER, PBR, 배당수익률 조회
    - PER: 0 < PER <= 20
    - PBR: 0 < PBR <= 3
    - DIV: >= 1.0%
    """
    try:
        date_str = target_date.strftime("%Y%m%d")
        
        # pykrx 호출 시 재시도 로직
        max_retries = 3
        for attempt in range(max_retries):
            try:
                df_fund = krx_stock.get_market_fundamental(date_str, date_str, code)
                
                if df_fund is None or df_fund.empty:
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue
                    return None, "재무데이터 없음"
                
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return None, f"조회실패({str(e)[:20]})"

        per = df_fund['PER'].iloc[-1] if 'PER' in df_fund.columns else 0
        pbr = df_fund['PBR'].iloc[-1] if 'PBR' in df_fund.columns else 0
        div = df_fund['DIV'].iloc[-1] if 'DIV' in df_fund.columns else 0

        # NaN 체크
        if pd.isna(per) or pd.isna(pbr) or pd.isna(div):
            return None, "데이터 누락"

        if per <= 0 or per > 20:
            return None, f"PER 부적합({per:.1f})"
        if pbr <= 0 or pbr > 3:
            return None, f"PBR 부적합({pbr:.1f})"
        if div < 1.0:
            return None, f"배당 미흡({div:.1f}%)"

        return {"per": round(per, 1), "pbr": round(pbr, 2), "div": round(div, 1)}, "통과"

    except Exception as e:
        return None, f"조회실패({str(e)[:20]})"

# ==========================================
# 🔍 메인 로직: 수급 + 정배열 + 지지구간 + 재무 분석
# ==========================================
def get_smart_money_top3():
    print("=" * 60)
    print("🕵️‍♂️ 펀드매니저 알고리즘 v3.0")
    print("   수급 + 120일선 정배열 + 지지구간 + 재무안전성")
    print("   📌 중장기 투자 (5060 세대 안전 추천)")
    print("=" * 60)

    target_date, reason = get_last_business_day()
    today = target_date.strftime("%Y-%m-%d")

    print(f"\n📅 분석 기준일: {target_date.strftime('%Y년 %m월 %d일 (%A)')}")
    print(f"   사유: {reason}")
    print(f"   현재 시각: {datetime.datetime.now().strftime('%Y년 %m월 %d일 %H시 %M분')}\n")

    print("🔍 코스피+코스닥 우량주 스캔 중...")
    try:
        df_kospi = fdr.StockListing('KOSPI')
        df_kosdaq = fdr.StockListing('KOSDAQ')

        if df_kospi is None or df_kosdaq is None:
            print("❌ 종목 리스트 로딩 실패")
            return None

        top_kospi = df_kospi.sort_values(by='Marcap', ascending=False).head(50)
        top_kosdaq = df_kosdaq.sort_values(by='Marcap', ascending=False).head(30)

        candidates_pool = pd.concat([top_kospi, top_kosdaq], ignore_index=True)
        print(f"✅ 코스피 50 + 코스닥 30 = 총 {len(candidates_pool)}개 종목 로딩 완료\n")
    except Exception as e:
        print(f"❌ 종목 리스트 로딩 실패: {e}")
        return None

    candidates = []
    start_date = (target_date - datetime.timedelta(days=180)).strftime("%Y-%m-%d")

    print("🔍 종목별 수급/정배열/지지구간/재무 정밀 분석 중...\n")

    count = 0
    skipped_alignment = 0
    skipped_financial = 0

    for idx, row in candidates_pool.iterrows():
        code = row['Code']
        name = row['Name']

        time.sleep(0.5)

        debug_mode = (count < 3)
        i_vol, f_vol = get_investor_data_naver(code, debug=debug_mode)

        if debug_mode:
            print(f"   [디버깅] {name}({code}): 기관 {i_vol:,}주 / 외국인 {f_vol:,}주")

        try:
            df = fdr.DataReader(code, start_date, today)
            if df is None or len(df) < 120:
                continue
        except Exception as e:
            if debug_mode:
                print(f"      ⚠️ {name}: 가격 데이터 로딩 실패 - {str(e)[:30]}")
            continue

        try:
            close_price = df['Close'].iloc[-1]
            i_amt = (i_vol * close_price) / 100000000
            f_amt = (f_vol * close_price) / 100000000
            total_amt = i_amt + f_amt
        except Exception as e:
            if debug_mode:
                print(f"      ⚠️ {name}: 수급 계산 실패")
            continue

        count += 1

        # 1차 필터: 수급 30억 이상
        if total_amt < 30:
            continue

        # 2차 필터: 120일선 위 (정배열)
        is_aligned, ma20, ma60, ma120, align_score = check_ma_alignment(df)
        if not is_aligned:
            skipped_alignment += 1
            if debug_mode:
                print(f"      ⛔ {name}: 120일선({ma120:,}원) 아래 → 제외")
            continue

        # 3차 필터: 재무 안전성 (PER/PBR/배당)
        fundamentals, fund_reason = get_financial_fundamentals(code, target_date)
        if fundamentals is None:
            skipped_financial += 1
            if debug_mode:
                print(f"   ⛔ {name}: 재무 미달 → {fund_reason}")
            continue

        # 지지구간 분석
        support, support_strength, support_desc = find_support_level(df)

        # 변동성 분석
        vol_type, target_rate, cut_rate = calculate_volatility(df)

        # 20일선 갭
        ma20_gap = (close_price - ma20) / ma20 * 100 if ma20 > 0 else 0

        if align_score == 4:
            align_status = "🟢 완벽한 정배열"
        elif align_score == 3:
            align_status = "🟡 준정배열"
        else:
            align_status = "🟠 120일선 위"

        candidates.append({
            "name": name,
            "price": int(close_price),
            "foreign": round(f_amt, 1),
            "inst": round(i_amt, 1),
            "total": round(total_amt, 1),
            "ma20": ma20,
            "ma60": ma60,
            "ma120": ma120,
            "ma20_gap": round(ma20_gap, 1),
            "align_score": align_score,
            "align_status": align_status,
            "support": support,
            "support_strength": support_strength,
            "support_desc": support_desc,
            "vol_type": vol_type,
            "target_rate": target_rate,
            "cut_rate": cut_rate,
            "per": fundamentals["per"],
            "pbr": fundamentals["pbr"],
            "div": fundamentals["div"],
        })

        print(f"   ✅ {name}: 수급 {total_amt:.1f}억 | {align_status} | PER {fundamentals['per']} | 배당 {fundamentals['div']}%")

    print(f"\n{'='*60}")
    print(f"📊 분석 결과: 총 {count}개 중 정배열 미달 {skipped_alignment}개 / 재무 미달 {skipped_financial}개 제외")

    if not candidates:
        print("❌ 조건을 만족하는 종목이 없습니다.")
        return None

    candidates.sort(key=lambda x: (x['align_score'], x['total']), reverse=True)
    top3 = candidates[:3]

    print(f"🏆 TOP {len(top3)} 최종 선정!")
    print(f"{'='*60}\n")

    for idx, item in enumerate(top3, 1):
        print(f"{idx}. {item['name']}: 수급 {item['total']}억 | {item['align_status']} | PER {item['per']} | 배당 {item['div']}%")

    return top3

# ==========================================
# 📝 리포트 작성
# ==========================================
def create_basic_report(top3):
    print("\n📝 투자 리포트 작성 중...")

    today_str = datetime.datetime.now().strftime("%Y년 %m월 %d일")

    report = f"""📈 [오늘의 수급 주도주 TOP 3]
📅 {today_str}
✅ 중장기 안전 투자 추천 (정배열 + 재무우량)

"""

    emoji = ["1️⃣", "2️⃣", "3️⃣"]

    for idx, item in enumerate(top3):
        target_p = int(item['price'] * (1 + item['target_rate']))
        cut_by_vol = int(item['price'] * (1 - item['cut_rate']))
        cut_by_supp = int(item['support'] * 0.98) if item['support'] > 0 else cut_by_vol
        cut_p = max(cut_by_vol, cut_by_supp)

        if item['total'] >= 500:
            strength = "🔥 역대급"
        elif item['total'] >= 200:
            strength = "💪 강력"
        elif item['total'] >= 100:
            strength = "✅ 양호"
        else:
            strength = "👀 개선"

        gap = item['ma20_gap']
        if gap > 5:
            strategy = f"추격 금지! 지지선({item['support']:,}원) 근처 눌림 매수"
        elif gap > 0:
            strategy = "보합~약보합 시 1차 30% 매수"
        elif gap > -3:
            strategy = f"20일선 지지 확인 후 매수 (지지: {item['support']:,}원)"
        else:
            strategy = f"지지선({item['support']:,}원) 반등 확인 후 진입"

        report += f"""{emoji[idx]} {item['name']} ({item['price']:,}원)
• 외국인 {item['foreign']}억 / 기관 {item['inst']}억 = {strength} {item['total']}억
• {item['align_status']} (20선 {item['ma20']:,} / 60선 {item['ma60']:,} / 120선 {item['ma120']:,})
• 재무: PER {item['per']} / PBR {item['pbr']} / 배당 {item['div']}%
• 지지선: {item['support']:,}원 ({item['support_strength']})
• 전략: {strategy}
• 목표 {target_p:,}원 / 손절 {cut_p:,}원

"""

    report += """━━━━━━━━━━━━━━━━
📌 TIP: 정배열 종목은 눌림목이 매수 기회!
⚠️ 투자는 본인 책임입니다."""

    print("✅ 리포트 작성 완료!")
    return report

# ==========================================
# 📱 카카오톡 전송
# ==========================================
def refresh_access_token():
    try:
        url = "https://kauth.kakao.com/oauth/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": KAKAO_API_KEY,
            "refresh_token": KAKAO_REFRESH_TOKEN
        }

        res = requests.post(url, data=data, timeout=10)
        if res.status_code != 200:
            print(f"❌ 토큰 갱신 실패: {res.text}")
            return None

        token_data = res.json()
        access_token = token_data.get('access_token')

        new_refresh = token_data.get('refresh_token')
        if new_refresh:
            print(f"⚠️ 새 리프레시 토큰 발급됨! 업데이트 필요:")
            print(f"   KAKAO_REFRESH_TOKEN = \"{new_refresh}\"")

        return access_token
    except Exception as e:
        print(f"❌ 토큰 갱신 중 오류: {e}")
        return None


def send_to_me(access_token, message):
    try:
        url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
        headers = {"Authorization": f"Bearer {access_token}"}
        data = {
            "template_object": json.dumps({
                "object_type": "text",
                "text": message,
                "link": {
                    "web_url": "https://m.stock.naver.com",
                    "mobile_web_url": "https://m.stock.naver.com"
                },
                "button_title": "종목 상세보기"
            })
        }

        res = requests.post(url, headers=headers, data=data, timeout=10)
        if res.status_code == 200:
            print("   ✅ 나에게 전송 완료!")
            return True
        else:
            print(f"   ❌ 전송 실패: {res.json()}")
            return False
    except Exception as e:
        print(f"   ❌ 전송 중 오류: {e}")
        return False


def send_kakao_to_all(message):
    print("\n" + "="*60)
    print("📱 카카오톡 메시지 전송 시작")
    print("="*60)

    print("\n1️⃣ 액세스 토큰 갱신 중...")
    access_token = refresh_access_token()
    if not access_token:
        print("⚠️ 카카오톡 전송을 건너뜁니다.")
        return
    print("   ✅ 토큰 갱신 완료")

    print("\n2️⃣ 나에게 전송 중...")
    send_to_me(access_token, message)

# ==========================================
# 🚀 메인 실행
# ==========================================
if __name__ == "__main__":
    print("\n🌅 매일 아침 수급 분석 시스템 v3.0\n")
    print("📌 필터링 조건:")
    print("   1. 수급 30억 이상")
    print("   2. 120일선 위 (정배열)")
    print("   3. 재무 안전성: PER ≤20 / PBR ≤3 / 배당 ≥1%")
    print("   4. 지지구간 분석")
    print()

    try:
        top3 = get_smart_money_top3()

        if top3:
            report = create_basic_report(top3)
            print("\n" + "="*60)
            print(report)
            print("="*60)
            send_kakao_to_all(report)
        else:
            print("\n❌ 조건을 만족하는 종목이 없습니다.")
    except Exception as e:
        print(f"\n❌ 실행 중 오류 발생: {e}")
        import traceback
        traceback.print_exc()
