import re
import csv
import io
import pdfplumber
from datetime import datetime, date
from utils.helpers import try_parse_date

CHASE_DATE_REGEX = re.compile(r"Opening/Closing Date\s*([\d/]+)\s*-\s*([\d/]+)")
CHASE_LINE_REGEX = re.compile(r"^(\d{1,2}/\d{1,2})\s+(.*)")
HSA_PERIOD_REGEX = re.compile(r"Period\s*:?\s*(\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4})\s*through\s*(\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4})", re.IGNORECASE)
AMOUNT_REGEX = re.compile(r"([\d,]+\.\d{2})(?=\s*$)")
HSA_LINE_REGEX = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.*?)\s+([\(]?[\d,]+\.\d{2}[\)]?)\s+[\d,]+\.\d{2}")

# Wells Fargo
WF_DATE_HEADER_REGEX = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4})\s+Page")
WF_BEGIN_BAL_REGEX = re.compile(r"Beginning balance on (\d{1,2}/\d{1,2})")
WF_END_BAL_REGEX = re.compile(r"Ending balance on (\d{1,2}/\d{1,2})")
WF_LINE_REGEX = re.compile(r"^(\d{1,2}/\d{1,2})\s+(.*?)\s+([\d,]+\.\d{2})")
WF_TEXT_LINE_REGEX = re.compile(r"^(\d{1,2}/\d{1,2})\s+(.*)")


def parse_fidelity_csv(file_stream):
    """
    Parses a Fidelity brokerage CSV export.
    Returns (transactions_list, min_date, max_date) importing:
    - INTEREST and DIVIDEND RECEIVED rows as direct income.
    - REDEMPTION PAYOUT rows for zero-coupon treasuries as discount income
      (redemption amount minus original purchase price).
    """
    INCOME_PREFIXES = ('INTEREST', 'DIVIDEND RECEIVED')
    TREASURY_KEYWORDS = ('TREAS', 'UNITED STATES', 'ZERO CPN')

    try:
        text = file_stream.read().decode('utf-8', errors='replace')
        lines = text.splitlines()

        header_idx = None
        for i, line in enumerate(lines):
            if 'Run Date' in line and 'Action' in line:
                header_idx = i
                break

        if header_idx is None:
            return None, None, None

        data_text = '\n'.join(lines[header_idx:])
        all_rows = list(csv.DictReader(io.StringIO(data_text)))

        # Pass 1: record purchase price for zero-coupon treasury buys keyed by CUSIP
        treasury_purchase = {}
        for row in all_rows:
            action = (row.get('Action') or '').strip().upper()
            if not action.startswith('YOU BOUGHT'):
                continue
            desc_upper = (row.get('Description') or '').upper()
            if not any(kw in desc_upper for kw in TREASURY_KEYWORDS):
                continue
            symbol = (row.get('Symbol') or '').strip()
            if not symbol:
                continue
            raw_amt = (row.get('Amount') or '').strip()
            try:
                amt = float(raw_amt.replace(',', ''))
            except ValueError:
                continue
            if amt < 0:
                treasury_purchase[symbol] = abs(amt)

        # Pass 2: collect income transactions
        transactions = []
        dates = []

        for row in all_rows:
            action = (row.get('Action') or '').strip()
            action_upper = action.upper()
            symbol = (row.get('Symbol') or '').strip()
            description = (row.get('Description') or '').strip()

            is_direct_income = any(action_upper.startswith(p) for p in INCOME_PREFIXES)
            is_treasury_redemption = (
                action_upper.startswith('REDEMPTION PAYOUT') and
                symbol in treasury_purchase
            )

            if not (is_direct_income or is_treasury_redemption):
                continue

            raw_amt = (row.get('Amount') or '').strip()
            if not raw_amt:
                continue
            try:
                redemption_amt = float(raw_amt.replace(',', ''))
            except ValueError:
                continue

            if is_treasury_redemption:
                amount = redemption_amt - treasury_purchase[symbol]
                if amount <= 0:
                    continue
            else:
                amount = redemption_amt
                if amount <= 0:
                    continue

            raw_date = (row.get('Run Date') or '').strip()
            try:
                tx_date = datetime.strptime(raw_date, '%m/%d/%Y').date()
            except ValueError:
                continue

            if symbol and len(symbol) <= 5 and symbol.isalpha():
                entity_name = symbol
            else:
                entity_name = description[:40].strip()

            transactions.append({'Date': tx_date, 'Description': entity_name, 'Amount': amount})
            dates.append(tx_date)

        if not dates:
            return None, None, None

        return transactions, min(dates), max(dates)
    except Exception as e:
        print(f"Fidelity CSV Parse Error: {e}")
        return None, None, None


def parse_chase_pdf(file_stream):
    """
    Returns a tuple: (transactions_list, period_start_date, period_end_date)
    or (None, None, None) on failure.
    """
    transactions = []
    period_start = None
    period_end = None
    try:
        with pdfplumber.open(file_stream) as pdf:
            page1_text = pdf.pages[0].extract_text()
            date_match = CHASE_DATE_REGEX.search(page1_text)
            if not date_match: return None, None, None
            
            period_start = datetime.strptime(date_match.group(1), '%m/%d/%y').date()
            period_end = datetime.strptime(date_match.group(2), '%m/%d/%y').date()
            end_year, end_month = period_end.year, period_end.month

            full_text = ""
            for p in pdf.pages:
                txt = p.extract_text()
                if txt: full_text += txt + "\n"
            lines = full_text.split('\n')
            current_multiplier = -1 
            for line in lines:
                if "PAYMENTS AND OTHER CREDITS" in line.upper(): current_multiplier = 1
                elif "PURCHASE" in line.upper(): current_multiplier = -1
                match = CHASE_LINE_REGEX.search(line)
                if match:
                    d_str, remainder = match.groups()
                    amt_match = AMOUNT_REGEX.search(remainder)
                    if amt_match:
                        amt_str = amt_match.group(1)
                        desc_raw = remainder[:amt_match.start()].strip()
                        if "Order Number" in desc_raw: continue
                        try:
                            t_month = int(d_str.split('/')[0])
                            year = end_year if t_month <= end_month else end_year - 1
                            dt = datetime.strptime(f"{t_month}/{d_str.split('/')[1]}/{year}", '%m/%d/%Y').date()
                            val = abs(float(amt_str.replace(',', '')))
                            final_amount = val * current_multiplier
                            transactions.append({'Date': dt, 'Description': desc_raw, 'Amount': final_amount})
                        except Exception: continue
        return transactions, period_start, period_end
    except Exception as e: 
        print(f"PDF Parse Error: {e}")
        return None, None, None
    
def parse_wellsfargo_pdf(file_stream):
    """
    Parses Wells Fargo PDF using Text Lines + Regex (No Tables).
    Looks for 'Transaction history' trigger, then parses lines with dates.
    """
    transactions = []
    period_start = None
    period_end = None
    statement_year = date.today().year
    
    transaction_section_found = False
    
    try:
        with pdfplumber.open(file_stream) as pdf:
            full_text = ""
            for p in pdf.pages: full_text += p.extract_text() + "\n"

            header_match = WF_DATE_HEADER_REGEX.search(full_text)
            if header_match:
                try:
                    dt_str = header_match.group(1)
                    statement_date_obj = datetime.strptime(dt_str, "%B %d, %Y").date()
                    statement_year = statement_date_obj.year
                except ValueError: pass
            
            start_match = WF_BEGIN_BAL_REGEX.search(full_text)
            end_match = WF_END_BAL_REGEX.search(full_text)
            if start_match and end_match:
                try:
                    p_start_str = f"{start_match.group(1)}/{statement_year}"
                    p_end_str = f"{end_match.group(1)}/{statement_year}"
                    period_start = datetime.strptime(p_start_str, "%m/%d/%Y").date()
                    period_end = datetime.strptime(p_end_str, "%m/%d/%Y").date()
                    if period_end < period_start:
                        period_start = period_start.replace(year=statement_year - 1)
                except ValueError: pass

            lines = full_text.split('\n')
            
            for line in lines:
                if "Transaction history" in line:
                    transaction_section_found = True
                    continue
                
                if not transaction_section_found:
                    continue
                
                if "Monthly service fee summary" in line:
                    break

                match = WF_TEXT_LINE_REGEX.search(line)
                if match:
                    date_str, rest = match.groups()
                    
                    numbers = re.findall(r"([\d,]+\.\d{2})", rest)
                    if not numbers: continue
                    
                    try:
                        raw_amount_str = numbers[0]
                        if len(numbers) >= 2:
                            raw_amount_str = numbers[0]
                        
                        amount = float(raw_amount_str.replace(',', ''))
                        
                        desc = rest.split(raw_amount_str)[0].strip()

                        income_keywords = ['DEPOSIT', 'PAYROLL', 'TRANSFER FROM', 'INTEREST', 'ZELLE FROM', 'VENMO PAYMENT']
                        
                        is_income = any(k in desc.upper() for k in income_keywords)
                        
                        if not is_income:
                            amount = -amount
                        
                        t_month = int(date_str.split('/')[0])
                        t_year = statement_year
                        if period_end and period_end.month < 6 and t_month > 6:
                            t_year -= 1
                        
                        t_date = datetime.strptime(f"{date_str}/{t_year}", "%m/%d/%Y").date()
                        
                        transactions.append({'Date': t_date, 'Description': desc, 'Amount': amount})
                        
                    except ValueError: continue

        return transactions, period_start, period_end
    except Exception as e:
        print(f"WF PDF Parse Error: {e}")
        return None, None, None

def parse_hsa_pdf(file_stream):
    """
    Returns a tuple: (transactions_list, period_start_date, period_end_date)
    """
    transactions = []
    period_start = None
    period_end = None
    try:
        with pdfplumber.open(file_stream) as pdf:
            full_text = ""
            for p in pdf.pages:
                txt = p.extract_text()
                if txt: full_text += txt + "\n"
            
            period_match = HSA_PERIOD_REGEX.search(full_text)
            print(period_match)
            if period_match:
                period_start = try_parse_date(period_match.group(1))
                period_end = try_parse_date(period_match.group(2))

            lines = full_text.split('\n')
            for line in lines:
                match = HSA_LINE_REGEX.search(line)
                if match:
                    date_str, desc, amt_str = match.groups()
                    try:
                        dt = datetime.strptime(date_str, '%m/%d/%Y').date()
                        is_negative = '(' in amt_str or ')' in amt_str
                        clean_amt = amt_str.replace('(', '').replace(')', '').replace(',', '')
                        amount = float(clean_amt)
                        
                        if is_negative:
                            amount = -amount
                        
                        if amount >= 0:
                            continue

                        transactions.append({'Date': dt, 'Description': desc.strip(), 'Amount': amount})
                    except ValueError:
                        continue 
        return transactions, period_start, period_end
    except Exception as e:
        print(f"HSA PDF Parse Error: {e}")
        return None, None, None
