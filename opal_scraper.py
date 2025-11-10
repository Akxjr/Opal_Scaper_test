import json
import asyncio
import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from playwright.async_api import async_playwright, TimeoutError as PWTimeout


# ------------------ Logging Configuration ------------------
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(log_dir, "scraper.log")
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
# ------------------------------------------------------------


def to_utc_and_local(time_text: str) -> tuple[str, str]:
    """Convert 'HH:MM' to local Sydney time and UTC ISO strings."""
    try:
        local_zone = timezone(timedelta(hours=11))  # AEDT +11
        hh, mm = time_text.split(":")
        local_time = datetime.now(local_zone).replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0
        )
        utc_time = local_time.astimezone(timezone.utc)
        return local_time.isoformat(), utc_time.isoformat()
    except Exception:
        now = datetime.now(timezone.utc)
        return now.isoformat(), now.isoformat()


async def get_first_visible_text(page, selector: str):
    """Return text of first visible element matching selector."""
    script = """
    (selector) => {
      const els = Array.from(document.querySelectorAll(selector || ''));
      for (const e of els) {
        try {
          const style = window.getComputedStyle(e);
          const visible = e.offsetWidth > 0 && e.offsetHeight > 0 &&
                          style.visibility !== 'hidden' && style.display !== 'none';
          if (visible) return e.innerText.trim();
        } catch (err) {}
      }
      return els.length ? els[0].innerText.trim() : null;
    }
    """
    return await page.evaluate(script, selector)


async def wait_for_value_change(page, selector: str, previous_value: str, max_wait: float = 5.0) -> str:
    
    start_time = asyncio.get_event_loop().time()
    while (asyncio.get_event_loop().time() - start_time) < max_wait:
        value = await get_first_visible_text(page, selector)
        if value and value != previous_value:
            return value
        await asyncio.sleep(0.3)
    return await get_first_visible_text(page, selector)


def parse_card_info_from_text(text: str) -> tuple[str | None, str | None]:
    
    card_name = None
    balance = None
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    for line in lines:
        if line.startswith('$'):
            balance = line
        else:
            is_amount = (line.replace('.', '').replace('-', '').replace(' ', '').isdigit() or
                        (line.replace('$', '').replace('.', '').replace('-', '').replace(' ', '').isdigit() and '.' in line))
            if not is_amount and line and 0 < len(line) < 50:
                card_name = line
    return (card_name.strip() if card_name else None, balance)


async def extract_trip_items(page, card_name: str) -> list[dict]:
    """Extract trip items for the currently active card."""
    trips: list[dict] = []
    try:
        await page.wait_for_selector(".card-activity-item", timeout=15000)
    except PWTimeout:
        logging.warning(f"No trip items found for {card_name}.")
        return trips

    items = await page.query_selector_all(".card-activity-item")
    logging.info(f"Found {len(items)} trips for card: {card_name}")

    for item in items:
        try:
            time_el = await item.query_selector(".date")
            from_el = await item.query_selector(".from")
            to_el = await item.query_selector(".to")
            amount_el = await item.query_selector(".amount span") or await item.query_selector(".amount")
            if not (time_el and from_el and to_el and amount_el):
                continue

            time_text = (await time_el.inner_text()).strip()
            from_loc = (await from_el.inner_text()).strip()
            to_loc = (await to_el.inner_text()).strip()

            amount_text = (await amount_el.inner_text()).strip()
            clean = amount_text.replace("$", "").replace(",", "").replace("-", "").strip()
            amt = float(clean) if clean else 0.0
            if "-" in amount_text:
                amt = -amt

            trip_type = "Unknown"
            try:
                icon = await item.query_selector(".card-activity-item-left tni-icon[iconname], .icons tni-icon[iconname]")
                if icon:
                    name = (await icon.get_attribute("iconname") or "").lower()
                    if "train" in name:
                        trip_type = "Train"
                    elif "metro" in name:
                        trip_type = "Metro"
                    elif "bus" in name:
                        trip_type = "Bus"
                    elif "ferry" in name:
                        trip_type = "Ferry"
                    elif "light" in name or "rail" in name:
                        trip_type = "Light Rail"
            except Exception:
                pass

            time_local, time_utc = to_utc_and_local(time_text)
            trips.append({
                "time_local": time_local,
                "time_utc": time_utc,
                "amount": amt,
                "currency": "AUD",
                "description": f"{from_loc} → {to_loc} ({trip_type})",
                "card_id": card_name,
                "trip_type": trip_type,
                "tap_on_location": from_loc,
                "tap_off_location": to_loc
            })
        except Exception as e:
            logging.warning(f"Skipped trip item: {e}")
    return trips


async def scrape_opal(username: str, password: str):
    """Main scraping routine with multi-card handling and login check."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context()
        page = await context.new_page()

        logging.info("Opening login page...")
        await page.goto("https://transportnsw.info/tickets-fares/opal-login#/login", wait_until="domcontentloaded")

        # try to login
        try:
            await page.wait_for_selector('#usernameCrtl', timeout=20000)
            await page.wait_for_selector('#passwordCtrl', timeout=20000)
        except PWTimeout:
            logging.error("Login form not found.")
            await browser.close()
            return

        logging.info("Filling login form...")
        await page.fill('#usernameCrtl', username)
        await page.fill('#passwordCtrl', password)
        await page.locator('button.opal-username-login:visible').first.click()

        logging.info("Waiting for login to finish...")
        await page.wait_for_load_state('networkidle')

        #  validation
        try:
            await page.wait_for_selector(".opal-selector__card-name", timeout=15000)
            logging.info("Login successful.")
        except PWTimeout:
            still_login = await page.is_visible('#usernameCrtl')
            error_msg = None
            try:
                # check for error message
                error_el = await page.query_selector(".alert-danger, .error, .login-error")
                if error_el:
                    error_msg = (await error_el.inner_text()).strip()
            except Exception:
                pass

            if still_login or error_msg:
                logging.error(f"Login failed. {error_msg or 'Please check your username or password.'}")
                await browser.close()
                return
            else:
                logging.error("Login timeout or unexpected page state.")
                await browser.close()
                return

        # Detect cards
        try:
            card_thumbs = await page.query_selector_all(".opal-selector__card")
        except Exception:
            card_thumbs = []
        num_cards = len(card_thumbs)
        logging.info(f"Detected {num_cards} card(s).")

        all_transactions = []
        balance_list = []

        if num_cards == 0:
            card_name = await get_first_visible_text(page, ".opal-selector__card-name")
            balance_text = await get_first_visible_text(page, ".opal-selector__card-value")
            bal = None
            if balance_text:
                txt = balance_text.replace("$", "").replace(",", "").strip()
                try:
                    bal = float(txt)
                except Exception:
                    pass
            if bal is not None:
                balance_list.append({"card_id": card_name, "balance": bal, "currency": "AUD"})
            trips = await extract_trip_items(page, card_name)
            all_transactions.extend(trips)
        else:
            prev_name = ""
            prev_balance_text = None
            for idx, thumb in enumerate(card_thumbs, start=1):
                try:
                    # try to get card info 
                    card_name_from_thumb = None
                    balance_from_thumb = None
                    try:
                        next_sibling = await thumb.evaluate_handle("el => el.nextElementSibling")
                        if next_sibling and next_sibling.as_element():
                            sibling_text = await next_sibling.as_element().inner_text()
                            if sibling_text:
                                card_name_from_thumb, balance_from_thumb = parse_card_info_from_text(sibling_text)
                    except Exception:
                        pass

                    await thumb.click()
                    await asyncio.sleep(1.5)

                    if card_name_from_thumb:
                        card_name = card_name_from_thumb
                    else:
                        if prev_name:
                            card_name = await wait_for_value_change(page, ".opal-selector__card-name", prev_name, max_wait=5.0)
                        else:
                            card_name = await get_first_visible_text(page, ".opal-selector__card-name")
                        if not card_name:
                            card_name = f"Card_{idx}"
                    prev_name = card_name

                    if balance_from_thumb:
                        balance_text = balance_from_thumb
                    else:
                        balance_text = await get_first_visible_text(page, ".opal-selector__card-value")

                    bal = None
                    if balance_text:
                        txt = balance_text.replace("$", "").replace(",", "").strip()
                        try:
                            bal = float(txt)
                        except Exception:
                            pass

                    logging.info(f"Processing card {idx}/{num_cards}: {card_name} - Balance: {bal}")
                    if bal is not None:
                        balance_list.append({"card_id": card_name, "balance": bal, "currency": "AUD"})

                    trips = await extract_trip_items(page, card_name)
                    all_transactions.extend(trips)

                except Exception as e:
                    logging.warning(f"Error processing card {idx}: {e}")
                    continue

        # write transactions to file
        with open("transactions.json", "w", encoding="utf-8") as f:
            json.dump(all_transactions, f, indent=2, ensure_ascii=False)

        # write balances to file
        if len(balance_list) <= 1:
            obj = {
                "balance": balance_list[0]["balance"] if balance_list else None,
                "currency": "AUD",
                "card_id": balance_list[0]["card_id"] if balance_list else None,
                "last_updated": datetime.now().isoformat()
            }
            with open("balance.json", "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=2, ensure_ascii=False)
        else:
            out = {"cards": balance_list, "last_updated": datetime.now().isoformat()}
            with open("balance.json", "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2, ensure_ascii=False)

        logging.info(f"Saved {len(all_transactions)} transactions.")
        logging.info("Saved balances to balance.json")
        await browser.close()


#demo mode
def run_demo():
    """Demo mode (no login, fake data)."""
    sample_transactions = [
        {
            "time_local": "2025-11-05T10:47:00+11:00",
            "time_utc": "2025-11-04T23:47:00Z",
            "amount": -22.26,
            "currency": "AUD",
            "description": "International → Rhodes (Train)",
            "card_id": "Jack",
            "trip_type": "Train",
            "tap_on_location": "International",
            "tap_off_location": "Rhodes"
        },
        {
            "time_local": "2025-11-05T09:15:00+11:00",
            "time_utc": "2025-11-04T22:15:00Z",
            "amount": -3.20,
            "currency": "AUD",
            "description": "Rhodes → Meadowbank (Bus)",
            "card_id": "Work Card",
            "trip_type": "Bus",
            "tap_on_location": "Rhodes",
            "tap_off_location": "Meadowbank"
        }
    ]
    sample_balances = {
        "cards": [
            {"card_id": "Jack", "balance": 9.7, "currency": "AUD"},
            {"card_id": "Work Card", "balance": 20.5, "currency": "AUD"}
        ],
        "last_updated": datetime.now().isoformat()
    }

    with open("transactions.json", "w", encoding="utf-8") as f:
        json.dump(sample_transactions, f, indent=2, ensure_ascii=False)
    with open("balance.json", "w", encoding="utf-8") as f:
        json.dump(sample_balances, f, indent=2, ensure_ascii=False)

    logging.info("Demo data written to transactions.json and balance.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="Run in demo mode")
    args = parser.parse_args()

    if args.demo:
        run_demo()
    else:
        username = input("Enter your Opal username: ").strip()
        password = input("Enter your password: ").strip()
        asyncio.run(scrape_opal(username, password))


if __name__ == "__main__":
    main()
