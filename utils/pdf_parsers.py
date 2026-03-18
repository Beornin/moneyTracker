import re
from datetime import datetime, date
import pdfplumber

# Regular expressions for parsing
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

def try_parse_date(date_str):
    if not date_str: return None
    date_str = date_str.strip()
    
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        pass

    clean_date_str = date_str.replace('.', '/').replace('-', '/')
    for fmt in ('%m/%d/%y', '%m/%d/%Y'):
        try:
            return datetime.strptime(clean_date_str, fmt).date()
        except ValueError:
            continue
    return None

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
