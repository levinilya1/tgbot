import os
import json
import requests
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
import anthropic
from datetime import datetime, timedelta, time as dtime
import pytz

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
ILYA_CHAT_ID = os.getenv("ILYA_CHAT_ID")
SPREADSHEET_ID = "148eOcsckeBzCP6S40JecmPJ0-y4yYUBDLqE8OjlfQNI"
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
credentials_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
if credentials_env:
    creds = Credentials.from_service_account_info(json.loads(credentials_env), scopes=SCOPES)
else:
    creds = Credentials.from_service_account_file("google_credentials.json", scopes=SCOPES)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

SYSTEM_PROMPT = """Ты личный ассистент врача-психиатра Ильи, который ведёт частную практику.

Всегда отвечай на русском языке. Общайся как с коллегой — без предисловий, лишних приветствий и фраз вроде "конечно, я помогу!". Отвечай по делу и кратко. Предлагай конкретные действия, а не общие рассуждения.

При вопросах о психиатрии, фармакологии и исследованиях опирайся на актуальные данные. Если информация спорная или устаревшая — говори об этом прямо. Всегда указывай уровень доказательности рекомендаций.

Помогай формулировать клинические заключения, писать рекомендации пациентам и структурировать клинические случаи с учётом психиатрического контекста.

Если я обсуждаю научные статьи — используй английские термины в скобках рядом с русскими.

Если я принимаю решение — указывай на слабые стороны и риски.

Запоминай мои предпочтения и привычки по ходу нашего разговора и адаптируйся к ним."""

history = {}


def setup_headers():
    patients_ws = spreadsheet.worksheet("Пациенты")
    if not patients_ws.get_all_values():
        patients_ws.append_row(["Псевдоним", "Следующая консультация", "Время", "Написать о самочувствии", "Заметки"])

    meds_ws = spreadsheet.worksheet("Препараты")
    if not meds_ws.get_all_values():
        meds_ws.append_row(["Псевдоним", "Препарат", "Дозировка", "Утро", "День", "Вечер", "Ночь", "Заметки"])

    sched_ws = spreadsheet.worksheet("Расписание")
    if not sched_ws.get_all_values():
        sched_ws.append_row(["Дата", "Время", "Псевдоним", "Заметки"])


def clean_rows(rows):
    return [{k.strip(): v for k, v in row.items()} for row in rows]


def get_all_patients():
    return clean_rows(spreadsheet.worksheet("Пациенты").get_all_records())


def get_patient(pseudonym):
    for row in get_all_patients():
        if row["Псевдоним"].lower() == pseudonym.lower():
            return row
    return None


def get_patient_medications(pseudonym):
    rows = clean_rows(spreadsheet.worksheet("Препараты").get_all_records())
    return [r for r in rows if r["Псевдоним"].lower() == pseudonym.lower()]


def get_upcoming_schedule(days=7):
    rows = clean_rows(spreadsheet.worksheet("Расписание").get_all_records())
    today = datetime.now(MOSCOW_TZ).date()
    result = []
    for row in rows:
        try:
            d = datetime.strptime(row["Дата"], "%d.%m.%Y").date()
            if today <= d <= today + timedelta(days=days):
                result.append((d, row))
        except (ValueError, KeyError):
            pass
    result.sort(key=lambda x: x[0])
    return [r for _, r in result]


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Доступные команды:\n\n"
        "Пациенты:\n"
        "/patients — список активных пациентов\n"
        "/card [псевдоним] — карточка пациента с диагнозом и препаратами\n"
        "/schedule — расписание консультаций на 7 дней\n\n"
        "Исследования:\n"
        "/pubmed [запрос] — поиск статей в PubMed\n"
        "/review [текст] — анализ научной статьи по пунктам\n\n"
        "Разное:\n"
        "/summary — резюме текущего разговора\n"
        "/clear — очистить память разговора\n"
    )
    await update.message.reply_text(text)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой chat_id: {update.message.chat_id}")


async def patients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = [r for r in get_all_patients() if r.get("Статус", "").strip() != "Архив"]
    if not rows:
        await update.message.reply_text("Активных пациентов нет.")
        return
    text = "Активные пациенты:\n\n"
    for r in rows:
        text += f"• {r['Псевдоним']}"
        if r.get("Следующая консультация"):
            t = r.get("Время", "")
            text += f" — {r['Следующая консультация']}"
            if t:
                text += f" в {t}"
        text += "\n"
    await update.message.reply_text(text)


async def card_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажи псевдоним. Пример:\n/card Альфа")
        return
    pseudonym = " ".join(context.args)
    patient = get_patient(pseudonym)
    if not patient:
        await update.message.reply_text(f"Пациент «{pseudonym}» не найден.")
        return

    lines = [f"Пациент: {patient['Псевдоним']}"]
    if patient.get("Диагноз"):
        lines.append(f"Диагноз: {patient['Диагноз']}")
    if patient.get("Предыдущая консультация"):
        lines.append(f"Предыдущая консультация: {patient['Предыдущая консультация']}")
    if patient.get("Следующая консультация"):
        t = patient.get("Время", "")
        lines.append(f"Следующая консультация: {patient['Следующая консультация']}" + (f" в {t}" if t else ""))
    if patient.get("Написать о самочувствии"):
        lines.append(f"Написать о самочувствии: {patient['Написать о самочувствии']}")
    if patient.get("Заметки"):
        lines.append(f"Заметки: {patient['Заметки']}")

    meds = get_patient_medications(pseudonym)
    if meds:
        lines.append("\nТерапия:")
        for m in meds:
            parts = []
            if m.get("Утро"): parts.append(f"утро — {m['Утро']}")
            if m.get("День"): parts.append(f"день — {m['День']}")
            if m.get("Вечер"): parts.append(f"вечер — {m['Вечер']}")
            if m.get("Ночь"): parts.append(f"ночь — {m['Ночь']}")
            schedule_str = ", ".join(parts)
            line = f"• {m['Препарат']} {m['Дозировка']}"
            if schedule_str:
                line += f" ({schedule_str})"
            if m.get("Заметки"):
                line += f"\n  {m['Заметки']}"
            lines.append(line)
    else:
        lines.append("\nПрепараты не указаны.")

    await update.message.reply_text("\n".join(lines))


async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upcoming = get_upcoming_schedule(days=7)
    if not upcoming:
        await update.message.reply_text("Консультаций на ближайшие 7 дней нет.")
        return
    text = "Расписание на 7 дней:\n\n"
    for r in upcoming:
        t = r.get("Время", "")
        text += f"• {r['Дата']}"
        if t:
            text += f" в {t}"
        text += f" — {r['Псевдоним']}"
        if r.get("Заметки"):
            text += f" ({r['Заметки']})"
        text += "\n"
    await update.message.reply_text(text)


async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    if not ILYA_CHAT_ID:
        return
    today = datetime.now(MOSCOW_TZ).date()
    today_str = today.strftime("%d.%m.%Y")
    tomorrow_str = (today + timedelta(days=1)).strftime("%d.%m.%Y")

    messages = []

    all_schedule = clean_rows(spreadsheet.worksheet("Расписание").get_all_records())

    today_appts = [r for r in all_schedule if r.get("Дата") == today_str]
    if today_appts:
        lines = ["Сегодня на приёме:"]
        for r in today_appts:
            t = r.get("Время", "")
            lines.append(f"• {r['Псевдоним']}" + (f" в {t}" if t else ""))
        messages.append("\n".join(lines))

    tomorrow_appts = [r for r in all_schedule if r.get("Дата") == tomorrow_str]
    if tomorrow_appts:
        lines = ["Завтра на приёме:"]
        for r in tomorrow_appts:
            t = r.get("Время", "")
            lines.append(f"• {r['Псевдоним']}" + (f" в {t}" if t else ""))
        messages.append("\n".join(lines))

    all_patients = clean_rows(spreadsheet.worksheet("Пациенты").get_all_records())
    wellbeing = [p for p in all_patients if p.get("Написать о самочувствии") == today_str and p.get("Статус", "").strip() != "Архив"]
    if wellbeing:
        lines = ["Сегодня написать о самочувствии:"]
        for p in wellbeing:
            lines.append(f"• {p['Псевдоним']}")
        messages.append("\n".join(lines))

    if messages:
        await context.bot.send_message(
            chat_id=int(ILYA_CHAT_ID),
            text="\n\n".join(messages)
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_message = update.message.text

    if user_id not in history:
        history[user_id] = []

    history[user_id].append({"role": "user", "content": user_message})

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        system=SYSTEM_PROMPT,
        messages=history[user_id]
    )

    response = message.content[0].text
    history[user_id].append({"role": "assistant", "content": response})

    if len(response) > 4096:
        for i in range(0, len(response), 4096):
            await update.message.reply_text(response[i:i+4096])
    else:
        await update.message.reply_text(response)


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in history or len(history[user_id]) == 0:
        await update.message.reply_text("Разговор пустой — нечего резюмировать.")
        return
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=history[user_id] + [{"role": "user", "content": "Резюмируй наш разговор кратко по пунктам."}]
    )
    await update.message.reply_text(message.content[0].text)


async def pubmed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Укажи запрос. Пример:\n/pubmed depression SSRIs treatment")
        return
    await update.message.reply_text("Ищу в PubMed...")
    search_response = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={"db": "pubmed", "term": query, "retmax": 5, "retmode": "json", "sort": "relevance"}
    )
    ids = search_response.json()["esearchresult"]["idlist"]
    if not ids:
        await update.message.reply_text("Ничего не найдено.")
        return
    data = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
        params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
    ).json()
    result = f"PubMed — {query}:\n\n"
    for pmid in ids:
        article = data["result"][pmid]
        authors = article.get("authors", [])
        result += f"• {article.get('title', 'Без названия')}\n"
        result += f"  {authors[0]['name'] if authors else 'Неизвестен'}, {article.get('pubdate', '')[:4]}\n"
        result += f"  https://pubmed.ncbi.nlm.nih.gov/{pmid}/\n\n"
    await update.message.reply_text(result)


async def review_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Вставь текст после команды. Пример:\n/review [текст статьи]")
        return
    await update.message.reply_text("Анализирую...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Резюмируй эту научную статью по пунктам: цель, методы, результаты, выводы, клиническая значимость.\n\n{text}"}]
    )
    response = message.content[0].text
    if len(response) > 4096:
        for i in range(0, len(response), 4096):
            await update.message.reply_text(response[i:i+4096])
    else:
        await update.message.reply_text(response)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    history[user_id] = []
    await update.message.reply_text("Память очищена.")


def main():
    setup_headers()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.job_queue.run_daily(
        daily_reminder,
        time=dtime(8, 0, tzinfo=MOSCOW_TZ),
    )

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("patients", patients_cmd))
    app.add_handler(CommandHandler("card", card_cmd))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(CommandHandler("pubmed", pubmed_cmd))
    app.add_handler(CommandHandler("review", review_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()
