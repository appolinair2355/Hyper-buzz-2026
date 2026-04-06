import re
import asyncio
import logging
import sys
import traceback
from typing import List, Optional, Dict
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError
from aiohttp import web

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    PREDICTION_CHANNEL_ID, PORT, API_POLL_INTERVAL,
    ALL_SUITS, SUIT_DISPLAY, TELEGRAM_SESSION,
    C1_SILENT_CHANNEL_ID, C2_SILENT_CHANNEL_ID,
    C3_SILENT_CHANNEL_ID, DOUBLE_CANAL_CHANNEL_ID
)
from utils import get_latest_results

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

client = None
prediction_channel_ok = False
current_game_number = 0
last_prediction_time: Optional[datetime] = None

pending_predictions: Dict[int, dict] = {}

prediction_history: List[Dict] = []
MAX_HISTORY_SIZE = 100

# Historique des prédictions silencieuses (C1 et C2)
silent_history: List[Dict] = []
MAX_SILENT_HISTORY = 150

api_results_cache: Dict[int, dict] = {}
player_processed_games: set = set()
last_prediction_game: int = 0
reset_done_for_cycle: bool = False

# ============================================================================
# COMPTEUR1 - B=5 | silencieux → canal après 2 pertes consécutives silencieuses
# Mapping: ♣→♦, ♦→♣, ♠→♥, ♥→♠
# ============================================================================

C1_B = 5
C1_SUIT_MAP = {'♣': '♦', '♦': '♣', '♠': '♥', '♥': '♠'}

c1_active: bool = True
c1_absences: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
c1_last_seen: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
c1_processed_games: set = set()
c1_consec_losses: int = 0
c1_pending_silent: Dict[int, dict] = {}

# ============================================================================
# COMPTEUR2 - B=8 | silencieux → canal après 1 perte silencieuse
# Mapping: ♥→♣, ♣→♥, ♠→♦, ♦→♠
# ============================================================================

C2_B = 8
C2_SUIT_MAP = {'♥': '♣', '♣': '♥', '♠': '♦', '♦': '♠'}

c2_active: bool = True
c2_absences: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
c2_last_seen: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
c2_processed_games: set = set()
c2_had_first_loss: bool = False
c2_pending_silent: Dict[int, dict] = {}

# ============================================================================
# COMPTEUR3 - B=5 | silencieux → double canal après 2 pertes consécutives
# Mapping: ❤→♣, ♣→❤, ♠→♦, ♦→♠
# ============================================================================

C3_B = 5
C3_SUIT_MAP = {'♥': '♣', '♣': '♥', '♠': '♦', '♦': '♠'}

c3_active: bool = True
c3_absences: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
c3_last_seen: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
c3_processed_games: set = set()
c3_consec_losses: int = 0
c3_pending_silent: Dict[int, dict] = {}

# ============================================================================
# INTERVALLES HORAIRES
# ============================================================================

BENIN_TZ = timezone(timedelta(hours=1))
prediction_intervals: List[Dict[str, int]] = []
intervals_enabled: bool = False

def is_prediction_allowed_now() -> bool:
    if not intervals_enabled or not prediction_intervals:
        return True
    now_benin = datetime.now(BENIN_TZ)
    current_total = now_benin.hour * 60 + now_benin.minute
    for interval in prediction_intervals:
        start_total = interval["start"] * 60
        end_total = interval["end"] * 60
        if start_total <= end_total:
            if start_total <= current_total < end_total:
                return True
        else:
            if current_total >= start_total or current_total < end_total:
                return True
    return False

def get_intervals_status_text() -> str:
    now_benin = datetime.now(BENIN_TZ)
    status = "✅ ON" if intervals_enabled else "❌ OFF"
    allowed = "✅ OUI" if is_prediction_allowed_now() else "🚫 NON"
    lines = [
        f"⏰ **Intervalles de prédiction**",
        f"Mode restriction: {status}",
        f"Heure Bénin actuelle: {now_benin.strftime('%H:%M')}",
        f"Prédiction autorisée: {allowed}",
        "",
    ]
    if prediction_intervals:
        lines.append("Intervalles configurés:")
        for i, iv in enumerate(prediction_intervals, 1):
            lines.append(f"  {i}. {iv['start']:02d}h00 → {iv['end']:02d}h00")
    else:
        lines.append("Aucun intervalle défini (toujours autorisé si mode OFF)")
    return "\n".join(lines)

# ============================================================================
# UTILITAIRES - Costumes
# ============================================================================

def normalize_suit(suit_emoji: str) -> str:
    return suit_emoji.replace('\ufe0f', '').replace('❤', '♥')

def player_suits_from_cards(player_cards: list) -> List[str]:
    suits = set()
    for card in player_cards:
        raw = card.get('S', '')
        normalized = normalize_suit(raw)
        if normalized in ALL_SUITS:
            suits.add(normalized)
    return list(suits)

def has_player_cards(result: dict) -> bool:
    return len(result.get('player_cards', [])) >= 2

# ============================================================================
# UTILITAIRES - Canaux
# ============================================================================

def normalize_channel_id(channel_id) -> Optional[int]:
    if not channel_id:
        return None
    s = str(channel_id)
    if s.startswith('-100'):
        return int(s)
    if s.startswith('-'):
        return int(s)
    return int(f"-100{s}")

async def resolve_channel(entity_id):
    try:
        if not entity_id:
            return None
        normalized = normalize_channel_id(entity_id)
        entity = await client.get_entity(normalized)
        return entity
    except Exception as e:
        logger.error(f"❌ Impossible de résoudre le canal {entity_id}: {e}")
        return None

# ============================================================================
# HISTORIQUE DES PRÉDICTIONS
# ============================================================================

def add_prediction_to_history(game_number: int, suit: str, triggered_by_suit: str, source: str = ""):
    global prediction_history
    prediction_history.insert(0, {
        'predicted_game': game_number,
        'suit': suit,
        'triggered_by': triggered_by_suit,
        'source': source,
        'predicted_at': datetime.now(),
        'status': 'en_cours',
        'result_game': None,
    })
    if len(prediction_history) > MAX_HISTORY_SIZE:
        prediction_history = prediction_history[:MAX_HISTORY_SIZE]

def add_silent_entry(source: str, pred_game: int, pred_suit: str, triggered_by: str,
                     consec_losses: int = 0, had_first_loss: bool = False,
                     sent_to_canal: bool = False, reason_canal: str = ""):
    """Ajoute une entrée dans l'historique silencieux."""
    global silent_history
    silent_history.insert(0, {
        'source': source,
        'pred_game': pred_game,
        'pred_suit': pred_suit,
        'triggered_by': triggered_by,
        'created_at': datetime.now(),
        'status': 'en_attente',
        'rattrapage': 0,
        'sent_to_canal': sent_to_canal,
        'reason_canal': reason_canal,
        'consec_losses_at_trigger': consec_losses,
        'had_first_loss_at_trigger': had_first_loss,
    })
    if len(silent_history) > MAX_SILENT_HISTORY:
        silent_history = silent_history[:MAX_SILENT_HISTORY]

def update_silent_entry_status(source: str, pred_game: int, status: str, rattrapage: int = 0):
    """Met à jour le statut d'une entrée silencieuse."""
    for entry in silent_history:
        if entry['source'] == source and entry['pred_game'] == pred_game and entry['status'] == 'en_attente':
            entry['status'] = status
            entry['rattrapage'] = rattrapage
            entry['resolved_at'] = datetime.now()
            break

def update_prediction_history_status(game_number: int, suit: str, status: str, result_game: int):
    for pred in prediction_history:
        if pred['predicted_game'] == game_number and pred['suit'] == suit:
            pred['status'] = status
            pred['result_game'] = result_game
            break

# ============================================================================
# ENVOI ET MISE À JOUR DES PRÉDICTIONS (CANAL)
# ============================================================================

async def send_prediction(game_number: int, suit: str, triggered_by_suit: str, source: str = "") -> Optional[int]:
    global last_prediction_time, last_prediction_game

    if not is_prediction_allowed_now():
        now_benin = datetime.now(BENIN_TZ)
        logger.info(
            f"⏰ Prédiction #{game_number} {suit} bloquée: hors intervalle "
            f"(heure Bénin: {now_benin.strftime('%H:%M')})"
        )
        return None

    if not PREDICTION_CHANNEL_ID:
        logger.error("❌ PREDICTION_CHANNEL_ID non configuré")
        return None

    prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
    if not prediction_entity:
        logger.error(f"❌ Canal prédiction inaccessible: {PREDICTION_CHANNEL_ID}")
        return None

    suit_display = SUIT_DISPLAY.get(suit, suit)
    msg = (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:⌛\n"
        f"Mode: Dogon 2"
    )

    try:
        sent = await client.send_message(prediction_entity, msg)
        last_prediction_time = datetime.now()
        last_prediction_game = game_number

        pending_predictions[game_number] = {
            'suit': suit,
            'triggered_by': triggered_by_suit,
            'source': source,
            'message_id': sent.id,
            'status': 'en_cours',
            'awaiting_rattrapage': 0,
            'sent_time': datetime.now(),
        }

        add_prediction_to_history(game_number, suit, triggered_by_suit, source)

        logger.info(f"✅ Prédiction canal envoyée: #{game_number} {suit} [{source}] (déclenché par {triggered_by_suit})")
        return sent.id

    except ChatWriteForbiddenError:
        logger.error(f"❌ Pas la permission d'écrire dans le canal {PREDICTION_CHANNEL_ID}")
        return None
    except UserBannedInChannelError:
        logger.error(f"❌ Bot banni du canal {PREDICTION_CHANNEL_ID}")
        return None
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        return None

async def update_prediction_message(game_number: int, status: str, trouve: bool, rattrapage: int = 0):
    global prediction_channel_ok

    if game_number not in pending_predictions:
        return

    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    suit_display = SUIT_DISPLAY.get(suit, suit)

    if trouve:
        rattrapage_icons = {0: "✅0️⃣", 1: "✅1️⃣", 2: "✅2️⃣"}
        result_icon = rattrapage_icons.get(rattrapage, f"✅{rattrapage}️⃣")
    else:
        result_icon = "❌"

    new_msg = (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:{result_icon}\n"
        f"Mode: Dogon 2"
    )

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error("❌ Canal prédiction inaccessible pour mise à jour")
            return

        await client.edit_message(prediction_entity, msg_id, new_msg)
        pred['status'] = status

        status_key = 'gagne' if trouve else 'perdu'
        update_prediction_history_status(game_number, suit, status_key, game_number)

        if trouve:
            logger.info(f"✅ Gagné: #{game_number} {suit} ({status})")
        else:
            logger.info(f"❌ Perdu: #{game_number} {suit}")

        del pending_predictions[game_number]

    except Exception as e:
        logger.error(f"❌ Erreur update message: {e}")

# ============================================================================
# ENVOI ET MISE À JOUR DES PRÉDICTIONS SILENCIEUSES
# ============================================================================

async def send_silent_prediction(
    game_number: int, suit: str, triggered_by: str,
    source: str, silent_channel_id: int,
    also_double_canal: bool = False
) -> dict:
    """Envoie une prédiction au canal silencieux dédié, et optionnellement au double canal."""
    suit_display = SUIT_DISPLAY.get(suit, suit)
    msg = (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:⌛\n"
        f"Mode: Dogon 2"
    )

    result = {'msg_id_silent': None, 'msg_id_double': None}

    entity = await resolve_channel(silent_channel_id)
    if entity:
        try:
            sent = await client.send_message(entity, msg)
            result['msg_id_silent'] = sent.id
            logger.info(f"🔕 [{source}] Silencieux #{game_number} {suit_display} → canal {silent_channel_id}")
        except Exception as e:
            logger.error(f"❌ [{source}] Erreur canal silencieux {silent_channel_id}: {e}")

    if also_double_canal and DOUBLE_CANAL_CHANNEL_ID:
        double_entity = await resolve_channel(DOUBLE_CANAL_CHANNEL_ID)
        if double_entity:
            try:
                sent2 = await client.send_message(double_entity, msg)
                result['msg_id_double'] = sent2.id
                logger.info(f"📢 [{source}] Double canal #{game_number} {suit_display} → {DOUBLE_CANAL_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ [{source}] Erreur double canal: {e}")

    return result


async def update_silent_message(
    pred: dict, silent_channel_id: int,
    original_game: int, suit: str,
    trouve: bool, rattrapage: int
):
    """Met à jour le message d'une prédiction silencieuse (rattrapage ou résultat final)."""
    suit_display = SUIT_DISPLAY.get(suit, suit)
    rattrapage_icons = {0: "✅0️⃣", 1: "✅1️⃣", 2: "✅2️⃣"}
    result_icon = rattrapage_icons.get(rattrapage, f"✅{rattrapage}️⃣") if trouve else "❌"

    new_msg = (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{original_game}\n"
        f"🃏Carte {suit_display}:{result_icon}\n"
        f"Mode: Dogon 2"
    )

    if pred.get('msg_id_silent') and silent_channel_id:
        entity = await resolve_channel(silent_channel_id)
        if entity:
            try:
                await client.edit_message(entity, pred['msg_id_silent'], new_msg)
            except Exception as e:
                logger.error(f"❌ Erreur update msg silencieux: {e}")

    if pred.get('msg_id_double') and DOUBLE_CANAL_CHANNEL_ID:
        double_entity = await resolve_channel(DOUBLE_CANAL_CHANNEL_ID)
        if double_entity:
            try:
                await client.edit_message(double_entity, pred['msg_id_double'], new_msg)
            except Exception as e:
                logger.error(f"❌ Erreur update msg double canal: {e}")


# ============================================================================
# VÉRIFICATION DYNAMIQUE - Prédictions canal
# ============================================================================

async def check_prediction_result_dynamic(game_number: int, player_suits: List[str], is_finished: bool):
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('awaiting_rattrapage', 0) == 0:
            target_suit = pred['suit']
            if target_suit in player_suits:
                logger.info(f"🔍 [DYN] #{game_number}: {target_suit} ✅ trouvé chez joueur")
                await update_prediction_message(game_number, 'gagne', True, 0)
            elif is_finished:
                pred['awaiting_rattrapage'] = 1
                logger.info(f"🔍 [DYN] #{game_number}: {target_suit} ❌ absent → rattrapage #{game_number + 1}")
            return

    for original_game, pred in list(pending_predictions.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        if awaiting <= 0:
            continue
        if game_number != original_game + awaiting:
            continue

        target_suit = pred['suit']

        if target_suit in player_suits:
            logger.info(f"🔍 [DYN] R{awaiting} #{game_number}: {target_suit} ✅ trouvé")
            await update_prediction_message(original_game, f'gagne_r{awaiting}', True, awaiting)
        elif is_finished:
            if awaiting < 2:
                pred['awaiting_rattrapage'] = awaiting + 1
                logger.info(f"🔍 [DYN] R{awaiting} #{game_number}: {target_suit} ❌ → R{awaiting+1} #{original_game + awaiting + 1}")
            else:
                logger.info(f"🔍 [DYN] R2 #{game_number}: {target_suit} ❌ → prédiction perdue")
                await update_prediction_message(original_game, 'perdu', False, 2)
        return

# ============================================================================
# VÉRIFICATION SILENCIEUSE - Compteur1
# ============================================================================

async def check_silent_result_c1(game_number: int, player_suits: List[str], is_finished: bool):
    global c1_consec_losses, c1_pending_silent

    to_delete = []

    for pred_game, pred in list(c1_pending_silent.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        target_game = pred_game + awaiting

        if game_number != target_game:
            continue

        target_suit = pred['suit']

        if target_suit in player_suits:
            logger.info(f"🔕 C1 #{pred_game} R{awaiting}: {target_suit} ✅ → consec_losses remis à 0")
            c1_consec_losses = 0
            await update_silent_message(pred, C1_SILENT_CHANNEL_ID, pred_game, target_suit, True, awaiting)
            update_silent_entry_status("C1", pred_game, "gagné", awaiting)
            to_delete.append(pred_game)
        elif is_finished:
            if awaiting < 2:
                pred['awaiting_rattrapage'] = awaiting + 1
                logger.info(f"🔕 C1 #{pred_game}: {target_suit} ❌ → R{awaiting+1} (jeu #{target_game+1})")
            else:
                c1_consec_losses += 1
                logger.info(f"🔕 C1 #{pred_game}: ❌ PERDU final → consec_losses={c1_consec_losses}")
                await update_silent_message(pred, C1_SILENT_CHANNEL_ID, pred_game, target_suit, False, awaiting)
                update_silent_entry_status("C1", pred_game, "perdu", awaiting)
                to_delete.append(pred_game)

    for k in to_delete:
        del c1_pending_silent[k]

# ============================================================================
# VÉRIFICATION SILENCIEUSE - Compteur2
# ============================================================================

async def check_silent_result_c2(game_number: int, player_suits: List[str], is_finished: bool):
    global c2_had_first_loss, c2_pending_silent

    to_delete = []

    for pred_game, pred in list(c2_pending_silent.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        target_game = pred_game + awaiting

        if game_number != target_game:
            continue

        target_suit = pred['suit']

        if target_suit in player_suits:
            logger.info(f"🔕 C2 #{pred_game} R{awaiting}: {target_suit} ✅ → had_first_loss=False")
            c2_had_first_loss = False
            await update_silent_message(pred, C2_SILENT_CHANNEL_ID, pred_game, target_suit, True, awaiting)
            update_silent_entry_status("C2", pred_game, "gagné", awaiting)
            to_delete.append(pred_game)
        elif is_finished:
            if awaiting < 2:
                pred['awaiting_rattrapage'] = awaiting + 1
                logger.info(f"🔕 C2 #{pred_game}: {target_suit} ❌ → R{awaiting+1} (jeu #{target_game+1})")
            else:
                c2_had_first_loss = True
                logger.info(f"🔕 C2 #{pred_game}: ❌ PERDU final → had_first_loss=True")
                await update_silent_message(pred, C2_SILENT_CHANNEL_ID, pred_game, target_suit, False, awaiting)
                update_silent_entry_status("C2", pred_game, "perdu", awaiting)
                to_delete.append(pred_game)

    for k in to_delete:
        del c2_pending_silent[k]

# ============================================================================
# VÉRIFICATION SILENCIEUSE - Compteur3
# ============================================================================

async def check_silent_result_c3(game_number: int, player_suits: List[str], is_finished: bool):
    global c3_consec_losses, c3_pending_silent

    to_delete = []

    for pred_game, pred in list(c3_pending_silent.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        target_game = pred_game + awaiting

        if game_number != target_game:
            continue

        target_suit = pred['suit']

        if target_suit in player_suits:
            logger.info(f"🔕 C3 #{pred_game} R{awaiting}: {target_suit} ✅ → consec_losses remis à 0")
            c3_consec_losses = 0
            await update_silent_message(pred, C3_SILENT_CHANNEL_ID, pred_game, target_suit, True, awaiting)
            update_silent_entry_status("C3", pred_game, "gagné", awaiting)
            to_delete.append(pred_game)
        elif is_finished:
            if awaiting < 2:
                pred['awaiting_rattrapage'] = awaiting + 1
                logger.info(f"🔕 C3 #{pred_game}: {target_suit} ❌ → R{awaiting+1} (jeu #{target_game+1})")
            else:
                c3_consec_losses += 1
                logger.info(f"🔕 C3 #{pred_game}: ❌ PERDU final → consec_losses={c3_consec_losses}")
                await update_silent_message(pred, C3_SILENT_CHANNEL_ID, pred_game, target_suit, False, awaiting)
                update_silent_entry_status("C3", pred_game, "perdu", awaiting)
                to_delete.append(pred_game)

    for k in to_delete:
        del c3_pending_silent[k]

# ============================================================================
# COMPTEUR1 - Logique principale
# ============================================================================

def get_c1_status_text() -> str:
    status = "✅ ON" if c1_active else "❌ OFF"
    lines = [
        f"📊 Compteur1: {status} | B={C1_B}",
        f"🔕 Pertes silencieuses consécutives: {c1_consec_losses}/2",
        f"🎯 Dernière prédiction canal: #{last_prediction_game}" if last_prediction_game else "🎯 Dernière prédiction canal: Aucune",
        "",
        "Progression des absences (cartes joueur):",
    ]
    for suit in ALL_SUITS:
        count = c1_absences.get(suit, 0)
        filled = '█' * count
        empty = '░' * max(0, C1_B - count)
        bar = f"[{filled}{empty}]"
        display = SUIT_DISPLAY.get(suit, suit)
        pred_display = SUIT_DISPLAY.get(C1_SUIT_MAP.get(suit, suit), suit)
        lines.append(f"{display} → {pred_display} : {bar} {count}/{C1_B}")
    if c1_pending_silent:
        lines.append(f"\n🔕 Prédictions silencieuses actives: {len(c1_pending_silent)}")
        for g, p in sorted(c1_pending_silent.items()):
            sd = SUIT_DISPLAY.get(p['suit'], p['suit'])
            ar = p.get('awaiting_rattrapage', 0)
            lines.append(f"  • #{g} {sd} (R{ar})")
    return "\n".join(lines)

async def process_compteur1(game_number: int, player_suits: List[str]):
    global c1_absences, c1_last_seen, c1_processed_games, c1_consec_losses, c1_pending_silent

    if not c1_active:
        return
    if game_number in c1_processed_games:
        return

    c1_processed_games.add(game_number)
    if len(c1_processed_games) > 200:
        c1_processed_games.discard(min(c1_processed_games))

    for suit in ALL_SUITS:
        if suit in player_suits:
            if c1_absences[suit] > 0:
                logger.info(f"📊 C1 {suit}: trouvé #{game_number} → reset (était {c1_absences[suit]})")
            c1_absences[suit] = 0
            c1_last_seen[suit] = game_number
        else:
            last_seen = c1_last_seen.get(suit, 0)
            if last_seen == 0 or game_number == last_seen + 1:
                c1_absences[suit] += 1
            else:
                c1_absences[suit] = 1
            c1_last_seen[suit] = game_number
            count = c1_absences[suit]
            logger.info(f"📊 C1 {suit}: absence {count}/{C1_B} (jeu #{game_number})")

            if count >= C1_B:
                pred_suit = C1_SUIT_MAP.get(suit, suit)
                pred_game = game_number + 1
                c1_absences[suit] = 0

                if pred_game not in c1_pending_silent:
                    also_double = (c1_consec_losses >= 2)
                    if also_double:
                        losses_snapshot = c1_consec_losses
                        c1_consec_losses = 0
                        reason = f"2 échecs consécutifs silencieux ({losses_snapshot} pertes)"
                        logger.info(
                            f"📢 C1 Double canal: {suit} absent {C1_B}x, {losses_snapshot} pertes "
                            f"→ #{pred_game} {pred_suit}"
                        )
                    else:
                        losses_snapshot = c1_consec_losses
                        reason = ""
                        logger.info(
                            f"🔕 C1 Silencieux: {suit} absent {C1_B}x → #{pred_game} {pred_suit} "
                            f"(consec_losses={c1_consec_losses})"
                        )

                    add_silent_entry(
                        source="C1",
                        pred_game=pred_game,
                        pred_suit=pred_suit,
                        triggered_by=suit,
                        consec_losses=losses_snapshot,
                        sent_to_canal=also_double,
                        reason_canal=reason
                    )
                    msg_ids = await send_silent_prediction(
                        pred_game, pred_suit, suit, "C1",
                        C1_SILENT_CHANNEL_ID,
                        also_double_canal=also_double
                    )
                    c1_pending_silent[pred_game] = {
                        'suit': pred_suit,
                        'triggered_by': suit,
                        'awaiting_rattrapage': 0,
                        'msg_id_silent': msg_ids['msg_id_silent'],
                        'msg_id_double': msg_ids['msg_id_double'],
                    }

# ============================================================================
# COMPTEUR2 - Logique principale
# ============================================================================

def get_c2_status_text() -> str:
    status = "✅ ON" if c2_active else "❌ OFF"
    lines = [
        f"📊 Compteur2: {status} | B={C2_B}",
        f"🔕 Perte silencieuse: {'✅ OUI (prochain → canal)' if c2_had_first_loss else '❌ NON (mode silencieux)'}",
        f"🎯 Dernière prédiction canal: #{last_prediction_game}" if last_prediction_game else "🎯 Dernière prédiction canal: Aucune",
        "",
        "Progression des absences (cartes joueur):",
    ]
    for suit in ALL_SUITS:
        count = c2_absences.get(suit, 0)
        filled = '█' * count
        empty = '░' * max(0, C2_B - count)
        bar = f"[{filled}{empty}]"
        display = SUIT_DISPLAY.get(suit, suit)
        pred_display = SUIT_DISPLAY.get(C2_SUIT_MAP.get(suit, suit), suit)
        lines.append(f"{display} → {pred_display} : {bar} {count}/{C2_B}")
    if c2_pending_silent:
        lines.append(f"\n🔕 Prédictions silencieuses actives: {len(c2_pending_silent)}")
        for g, p in sorted(c2_pending_silent.items()):
            sd = SUIT_DISPLAY.get(p['suit'], p['suit'])
            ar = p.get('awaiting_rattrapage', 0)
            lines.append(f"  • #{g} {sd} (R{ar})")
    return "\n".join(lines)

async def process_compteur2(game_number: int, player_suits: List[str]):
    global c2_absences, c2_last_seen, c2_processed_games, c2_had_first_loss, c2_pending_silent

    if not c2_active:
        return
    if game_number in c2_processed_games:
        return

    c2_processed_games.add(game_number)
    if len(c2_processed_games) > 200:
        c2_processed_games.discard(min(c2_processed_games))

    for suit in ALL_SUITS:
        if suit in player_suits:
            if c2_absences[suit] > 0:
                logger.info(f"📊 C2 {suit}: trouvé #{game_number} → reset (était {c2_absences[suit]})")
            c2_absences[suit] = 0
            c2_last_seen[suit] = game_number
        else:
            last_seen = c2_last_seen.get(suit, 0)
            if last_seen == 0 or game_number == last_seen + 1:
                c2_absences[suit] += 1
            else:
                c2_absences[suit] = 1
            c2_last_seen[suit] = game_number
            count = c2_absences[suit]
            logger.info(f"📊 C2 {suit}: absence {count}/{C2_B} (jeu #{game_number})")

            if count >= C2_B:
                pred_suit = C2_SUIT_MAP.get(suit, suit)
                pred_game = game_number + 1
                c2_absences[suit] = 0

                if pred_game not in c2_pending_silent:
                    also_double = c2_had_first_loss
                    if also_double:
                        c2_had_first_loss = False
                        reason = "1 échec silencieux (seuil B=8 atteint après 1 perte)"
                        logger.info(
                            f"📢 C2 Double canal: {suit} absent {C2_B}x, 1 perte silenc "
                            f"→ #{pred_game} {pred_suit}"
                        )
                    else:
                        reason = ""
                        logger.info(
                            f"🔕 C2 Silencieux: {suit} absent {C2_B}x → #{pred_game} {pred_suit}"
                        )

                    add_silent_entry(
                        source="C2",
                        pred_game=pred_game,
                        pred_suit=pred_suit,
                        triggered_by=suit,
                        had_first_loss=also_double,
                        sent_to_canal=also_double,
                        reason_canal=reason
                    )
                    msg_ids = await send_silent_prediction(
                        pred_game, pred_suit, suit, "C2",
                        C2_SILENT_CHANNEL_ID,
                        also_double_canal=also_double
                    )
                    c2_pending_silent[pred_game] = {
                        'suit': pred_suit,
                        'triggered_by': suit,
                        'awaiting_rattrapage': 0,
                        'msg_id_silent': msg_ids['msg_id_silent'],
                        'msg_id_double': msg_ids['msg_id_double'],
                    }

# ============================================================================
# COMPTEUR3 - Logique principale
# ============================================================================

def get_c3_status_text() -> str:
    status = "✅ ON" if c3_active else "❌ OFF"
    lines = [
        f"📊 Compteur3: {status} | B={C3_B}",
        f"🔕 Pertes silencieuses consécutives: {c3_consec_losses}/2",
        "",
        "Progression des absences (cartes joueur):",
    ]
    for suit in ALL_SUITS:
        count = c3_absences.get(suit, 0)
        filled = '█' * count
        empty = '░' * max(0, C3_B - count)
        bar = f"[{filled}{empty}]"
        display = SUIT_DISPLAY.get(suit, suit)
        pred_display = SUIT_DISPLAY.get(C3_SUIT_MAP.get(suit, suit), suit)
        lines.append(f"{display} → {pred_display} : {bar} {count}/{C3_B}")
    if c3_pending_silent:
        lines.append(f"\n🔕 Prédictions silencieuses actives: {len(c3_pending_silent)}")
        for g, p in sorted(c3_pending_silent.items()):
            sd = SUIT_DISPLAY.get(p['suit'], p['suit'])
            ar = p.get('awaiting_rattrapage', 0)
            lines.append(f"  • #{g} {sd} (R{ar})")
    return "\n".join(lines)

async def process_compteur3(game_number: int, player_suits: List[str]):
    global c3_absences, c3_last_seen, c3_processed_games, c3_consec_losses, c3_pending_silent

    if not c3_active:
        return
    if game_number in c3_processed_games:
        return

    c3_processed_games.add(game_number)
    if len(c3_processed_games) > 200:
        c3_processed_games.discard(min(c3_processed_games))

    for suit in ALL_SUITS:
        if suit in player_suits:
            if c3_absences[suit] > 0:
                logger.info(f"📊 C3 {suit}: trouvé #{game_number} → reset (était {c3_absences[suit]})")
            c3_absences[suit] = 0
            c3_last_seen[suit] = game_number
        else:
            last_seen = c3_last_seen.get(suit, 0)
            if last_seen == 0 or game_number == last_seen + 1:
                c3_absences[suit] += 1
            else:
                c3_absences[suit] = 1
            c3_last_seen[suit] = game_number
            count = c3_absences[suit]
            logger.info(f"📊 C3 {suit}: absence {count}/{C3_B} (jeu #{game_number})")

            if count >= C3_B:
                pred_suit = C3_SUIT_MAP.get(suit, suit)
                pred_game = game_number + 1
                c3_absences[suit] = 0

                if pred_game not in c3_pending_silent:
                    also_double = (c3_consec_losses >= 2)
                    if also_double:
                        losses_snapshot = c3_consec_losses
                        c3_consec_losses = 0
                        reason = f"2 échecs consécutifs silencieux ({losses_snapshot} pertes)"
                        logger.info(
                            f"📢 C3 Double canal: {suit} absent {C3_B}x, {losses_snapshot} pertes "
                            f"→ #{pred_game} {pred_suit}"
                        )
                    else:
                        losses_snapshot = c3_consec_losses
                        reason = ""
                        logger.info(
                            f"🔕 C3 Silencieux: {suit} absent {C3_B}x → #{pred_game} {pred_suit} "
                            f"(consec_losses={c3_consec_losses})"
                        )

                    add_silent_entry(
                        source="C3",
                        pred_game=pred_game,
                        pred_suit=pred_suit,
                        triggered_by=suit,
                        consec_losses=losses_snapshot,
                        sent_to_canal=also_double,
                        reason_canal=reason
                    )
                    msg_ids = await send_silent_prediction(
                        pred_game, pred_suit, suit, "C3",
                        C3_SILENT_CHANNEL_ID,
                        also_double_canal=also_double
                    )
                    c3_pending_silent[pred_game] = {
                        'suit': pred_suit,
                        'triggered_by': suit,
                        'awaiting_rattrapage': 0,
                        'msg_id_silent': msg_ids['msg_id_silent'],
                        'msg_id_double': msg_ids['msg_id_double'],
                    }

# ============================================================================
# BOUCLE DE POLLING API
# ============================================================================

async def api_polling_loop():
    global current_game_number, api_results_cache, player_processed_games
    global reset_done_for_cycle

    loop = asyncio.get_event_loop()
    logger.info(f"🔄 Polling API dynamique démarré (intervalle: {API_POLL_INTERVAL}s)")

    while True:
        try:
            results = await loop.run_in_executor(None, get_latest_results)

            if results:
                for result in results:
                    game_number = result["game_number"]
                    is_finished = result["is_finished"]
                    player_cards = result.get("player_cards", [])

                    api_results_cache[game_number] = result

                    player_suits = player_suits_from_cards(player_cards)
                    ready = len(player_cards) >= 2

                    if not ready:
                        continue

                    current_game_number = game_number

                    p_display = " ".join(SUIT_DISPLAY.get(s, s) for s in player_suits) or "—"

                    # 1. Vérification dynamique prédictions canal
                    await check_prediction_result_dynamic(game_number, player_suits, is_finished)

                    # 2. Vérification silencieuse
                    await check_silent_result_c1(game_number, player_suits, is_finished)
                    await check_silent_result_c2(game_number, player_suits, is_finished)
                    await check_silent_result_c3(game_number, player_suits, is_finished)

                    # 3. Traitement des compteurs dès que joueur a ses cartes
                    if game_number not in player_processed_games and ready:
                        player_processed_games.add(game_number)
                        if len(player_processed_games) > 500:
                            player_processed_games.discard(min(player_processed_games))

                        logger.info(
                            f"🃏 Jeu #{game_number} | Joueur: {p_display} "
                            f"| Terminé: {is_finished}"
                        )
                        await process_compteur1(game_number, player_suits)
                        await process_compteur2(game_number, player_suits)
                        await process_compteur3(game_number, player_suits)

                    # 4. Reset automatique sur partie #1440
                    if game_number == 1440 and is_finished and not reset_done_for_cycle:
                        reset_done_for_cycle = True
                        logger.info("🔄 Reset automatique: partie #1440 terminée")
                        await perform_full_reset("Reset automatique (partie #1440 terminée)")

                    if game_number < 100 and reset_done_for_cycle:
                        reset_done_for_cycle = False
                        logger.info("🔄 Nouveau cycle détecté → flag reset remis à zéro")

                if len(api_results_cache) > 300:
                    oldest = min(api_results_cache.keys())
                    del api_results_cache[oldest]

        except Exception as e:
            logger.error(f"❌ Erreur polling API: {e}")
            logger.error(traceback.format_exc())

        await asyncio.sleep(API_POLL_INTERVAL)

# ============================================================================
# RESET COMPLET
# ============================================================================

async def perform_full_reset(reason: str):
    global pending_predictions, last_prediction_time, last_prediction_game
    global player_processed_games, api_results_cache, reset_done_for_cycle
    global c1_absences, c1_last_seen, c1_processed_games, c1_consec_losses, c1_pending_silent
    global c2_absences, c2_last_seen, c2_processed_games, c2_had_first_loss, c2_pending_silent
    global c3_absences, c3_last_seen, c3_processed_games, c3_consec_losses, c3_pending_silent

    stats = len(pending_predictions)
    pending_predictions.clear()
    last_prediction_time = None
    last_prediction_game = 0
    player_processed_games = set()
    api_results_cache = {}

    c1_absences = {suit: 0 for suit in ALL_SUITS}
    c1_last_seen = {suit: 0 for suit in ALL_SUITS}
    c1_processed_games = set()
    c1_consec_losses = 0
    c1_pending_silent = {}

    c2_absences = {suit: 0 for suit in ALL_SUITS}
    c2_last_seen = {suit: 0 for suit in ALL_SUITS}
    c2_processed_games = set()
    c2_had_first_loss = False
    c2_pending_silent = {}

    c3_absences = {suit: 0 for suit in ALL_SUITS}
    c3_last_seen = {suit: 0 for suit in ALL_SUITS}
    c3_processed_games = set()
    c3_consec_losses = 0
    c3_pending_silent = {}

    global silent_history
    silent_history = []

    logger.info(f"🔄 {reason} - {stats} prédictions cleared")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_compteur1(event):
    global c1_active, c1_absences, c1_last_seen, c1_processed_games
    global c1_consec_losses, c1_pending_silent

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(get_c1_status_text())
        return

    arg = parts[1].lower()

    if arg == 'on':
        c1_active = True
        c1_absences = {suit: 0 for suit in ALL_SUITS}
        c1_last_seen = {suit: 0 for suit in ALL_SUITS}
        c1_processed_games = set()
        c1_consec_losses = 0
        c1_pending_silent = {}
        await event.respond(f"✅ Compteur1 ACTIVÉ | B={C1_B}\n\n" + get_c1_status_text())

    elif arg == 'off':
        c1_active = False
        await event.respond("❌ Compteur1 DÉSACTIVÉ")

    elif arg == 'reset':
        c1_absences = {suit: 0 for suit in ALL_SUITS}
        c1_last_seen = {suit: 0 for suit in ALL_SUITS}
        c1_processed_games = set()
        c1_consec_losses = 0
        c1_pending_silent = {}
        await event.respond("🔄 Compteur1 remis à zéro\n\n" + get_c1_status_text())

    else:
        await event.respond(
            "📊 **COMPTEUR1 - Aide**\n\n"
            f"B={C1_B} | Silencieux → canal après 2 pertes consécutives\n\n"
            "Mapping: ♣️→♦️ | ♦️→♣️ | ♠️→❤️ | ❤️→♠️\n\n"
            "`/compteur1` — Afficher l'état\n"
            "`/compteur1 on` — Activer\n"
            "`/compteur1 off` — Désactiver\n"
            "`/compteur1 reset` — Remettre à zéro"
        )

async def cmd_compteur2(event):
    global c2_active, c2_absences, c2_last_seen, c2_processed_games
    global c2_had_first_loss, c2_pending_silent

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(get_c2_status_text())
        return

    arg = parts[1].lower()

    if arg == 'on':
        c2_active = True
        c2_absences = {suit: 0 for suit in ALL_SUITS}
        c2_last_seen = {suit: 0 for suit in ALL_SUITS}
        c2_processed_games = set()
        c2_had_first_loss = False
        c2_pending_silent = {}
        await event.respond(f"✅ Compteur2 ACTIVÉ | B={C2_B}\n\n" + get_c2_status_text())

    elif arg == 'off':
        c2_active = False
        await event.respond("❌ Compteur2 DÉSACTIVÉ")

    elif arg == 'reset':
        c2_absences = {suit: 0 for suit in ALL_SUITS}
        c2_last_seen = {suit: 0 for suit in ALL_SUITS}
        c2_processed_games = set()
        c2_had_first_loss = False
        c2_pending_silent = {}
        await event.respond("🔄 Compteur2 remis à zéro\n\n" + get_c2_status_text())

    else:
        await event.respond(
            "📊 **COMPTEUR2 - Aide**\n\n"
            f"B={C2_B} | Silencieux → canal après 1 perte\n\n"
            "Mapping: ❤️→♣️ | ♣️→❤️ | ♠️→♦️ | ♦️→♠️\n\n"
            "`/compteur2` — Afficher l'état\n"
            "`/compteur2 on` — Activer\n"
            "`/compteur2 off` — Désactiver\n"
            "`/compteur2 reset` — Remettre à zéro"
        )

async def cmd_compteur3(event):
    global c3_active, c3_absences, c3_last_seen, c3_processed_games
    global c3_consec_losses, c3_pending_silent

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(get_c3_status_text())
        return

    arg = parts[1].lower()

    if arg == 'on':
        c3_active = True
        c3_absences = {suit: 0 for suit in ALL_SUITS}
        c3_last_seen = {suit: 0 for suit in ALL_SUITS}
        c3_processed_games = set()
        c3_consec_losses = 0
        c3_pending_silent = {}
        await event.respond(f"✅ Compteur3 ACTIVÉ | B={C3_B}\n\n" + get_c3_status_text())

    elif arg == 'off':
        c3_active = False
        await event.respond("❌ Compteur3 DÉSACTIVÉ")

    elif arg == 'reset':
        c3_absences = {suit: 0 for suit in ALL_SUITS}
        c3_last_seen = {suit: 0 for suit in ALL_SUITS}
        c3_processed_games = set()
        c3_consec_losses = 0
        c3_pending_silent = {}
        await event.respond("🔄 Compteur3 remis à zéro\n\n" + get_c3_status_text())

    else:
        await event.respond(
            "📊 **COMPTEUR3 - Aide**\n\n"
            f"B={C3_B} | Silencieux → double canal après 2 pertes consécutives\n\n"
            "Mapping: ❤️→♣️ | ♣️→❤️ | ♠️→♦️ | ♦️→♠️\n\n"
            "`/compteur3` — Afficher l'état\n"
            "`/compteur3 on` — Activer\n"
            "`/compteur3 off` — Désactiver\n"
            "`/compteur3 reset` — Remettre à zéro"
        )


async def cmd_silencieux(event):
    """Affiche l'historique de toutes les prédictions silencieuses avec leurs raisons."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()
    show_all = len(parts) > 1 and parts[1].lower() == 'all'
    max_show = 50 if show_all else 20

    lines = [
        "🔕 **PRÉDICTIONS SILENCIEUSES**",
        "═══════════════════════════════════════",
        ""
    ]

    # ── Prédictions silencieuses actives (en attente de résultat) ─────────────
    actives_c1 = [(g, p) for g, p in sorted(c1_pending_silent.items())]
    actives_c2 = [(g, p) for g, p in sorted(c2_pending_silent.items())]
    actives_c3 = [(g, p) for g, p in sorted(c3_pending_silent.items())]

    if actives_c1 or actives_c2 or actives_c3:
        lines.append("**⏳ EN COURS (non résolues) :**")
        for g, p in actives_c1:
            sd = SUIT_DISPLAY.get(p['suit'], p['suit'])
            td = SUIT_DISPLAY.get(p['triggered_by'], p['triggered_by'])
            ar = p.get('awaiting_rattrapage', 0)
            ratt = f" | R{ar}" if ar > 0 else ""
            lines.append(
                f"  🔕 **[C1]** Game #N{g}{ratt}\n"
                f"     🃏 Prédit: {sd} | Déclenché: {td} absent {C1_B}x\n"
                f"     📌 Pertes consécutives: {c1_consec_losses}/2"
            )
        for g, p in actives_c2:
            sd = SUIT_DISPLAY.get(p['suit'], p['suit'])
            td = SUIT_DISPLAY.get(p['triggered_by'], p['triggered_by'])
            ar = p.get('awaiting_rattrapage', 0)
            ratt = f" | R{ar}" if ar > 0 else ""
            lines.append(
                f"  🔕 **[C2]** Game #N{g}{ratt}\n"
                f"     🃏 Prédit: {sd} | Déclenché: {td} absent {C2_B}x\n"
                f"     📌 Perte précédente: {'OUI' if c2_had_first_loss else 'NON'}"
            )
        for g, p in actives_c3:
            sd = SUIT_DISPLAY.get(p['suit'], p['suit'])
            td = SUIT_DISPLAY.get(p['triggered_by'], p['triggered_by'])
            ar = p.get('awaiting_rattrapage', 0)
            ratt = f" | R{ar}" if ar > 0 else ""
            lines.append(
                f"  🔕 **[C3]** Game #N{g}{ratt}\n"
                f"     🃏 Prédit: {sd} | Déclenché: {td} absent {C3_B}x\n"
                f"     📌 Pertes consécutives: {c3_consec_losses}/2"
            )
        lines.append("")

    # ── Historique ─────────────────────────────────────────────────────────────
    if not silent_history:
        if not actives_c1 and not actives_c2 and not actives_c3:
            lines.append("Aucune prédiction silencieuse enregistrée.")
    else:
        lines.append(f"**📜 HISTORIQUE** (dernières {min(len(silent_history), max_show)}) :")
        lines.append("")

        for i, entry in enumerate(silent_history[:max_show], 1):
            src = entry['source']
            g = entry['pred_game']
            sd = SUIT_DISPLAY.get(entry['pred_suit'], entry['pred_suit'])
            td = SUIT_DISPLAY.get(entry['triggered_by'], entry['triggered_by'])
            t = entry['created_at'].strftime('%H:%M:%S')
            status = entry['status']
            sent_canal = entry.get('sent_to_canal', False)
            reason_canal = entry.get('reason_canal', '')
            ratt = entry.get('rattrapage', 0)

            if status == 'en_attente':
                status_icon = "⏳"
                status_str = "En cours..."
            elif status == 'gagné':
                r_str = f" (R{ratt})" if ratt > 0 else ""
                status_icon = "✅"
                status_str = f"GAGNÉ{r_str}"
            else:
                status_icon = "❌"
                status_str = "PERDU"

            if sent_canal:
                type_str = "📢 **→ ENVOYÉ AU CANAL**"
                if src == "C1":
                    raison_str = f"⚠️ Raison: {reason_canal}"
                else:
                    raison_str = f"⚠️ Raison: {reason_canal}"
            else:
                b_val = C1_B if src == "C1" else C2_B
                if src == "C1":
                    cl = entry.get('consec_losses_at_trigger', 0)
                    raison_str = f"📌 Raison silence: {cl}/2 pertes silencieuses"
                else:
                    hfl = entry.get('had_first_loss_at_trigger', False)
                    raison_str = f"📌 Raison silence: {'1 perte déjà' if hfl else 'pas encore de perte'}"
                type_str = "🔕 Silencieux"

            lines.append(
                f"{i}. 🕐 `{t}` | **[{src}]** Game #N{g} | {status_icon} {status_str}\n"
                f"   🃏 {td} absent → prédit {sd}\n"
                f"   {type_str}\n"
                f"   {raison_str}"
            )
            lines.append("")

    if len(silent_history) > max_show and not show_all:
        lines.append(f"_... {len(silent_history) - max_show} entrées supplémentaires. Tapez `/silencieux all` pour tout voir._")

    lines.append("═══════════════════════════════════════")
    lines.append(
        f"\n📊 **Résumé actuel:**\n"
        f"C1 — Pertes consécutives: **{c1_consec_losses}/2** "
        f"{'→ prochain → double canal ✅' if c1_consec_losses >= 2 else '→ encore silencieux'}\n"
        f"C2 — Perte précédente: **{'OUI' if c2_had_first_loss else 'NON'}** "
        f"{'→ prochain → double canal ✅' if c2_had_first_loss else '→ encore silencieux'}\n"
        f"C3 — Pertes consécutives: **{c3_consec_losses}/2** "
        f"{'→ prochain → double canal ✅' if c3_consec_losses >= 2 else '→ encore silencieux'}"
    )

    full_text = "\n".join(lines)
    if len(full_text) > 4000:
        chunks = [full_text[i:i+4000] for i in range(0, len(full_text), 4000)]
        for chunk in chunks:
            await event.respond(chunk)
    else:
        await event.respond(full_text)


async def cmd_attente(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond(
        "ℹ️ **MODE ATTENTE**\n\n"
        "Les compteurs gèrent maintenant automatiquement les prédictions silencieuses.\n\n"
        "• **Compteur1** : silencieux pendant 2 pertes consécutives, puis envoi au canal\n"
        "• **Compteur2** : silencieux 1 fois, puis envoi au canal après 1 perte"
    )

async def cmd_history(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    if not prediction_history:
        await event.respond("📜 Aucune prédiction dans l'historique.")
        return

    lines = [
        "📜 **HISTORIQUE DES PRÉDICTIONS**",
        "═══════════════════════════════════════",
        ""
    ]

    for i, pred in enumerate(prediction_history[:20], 1):
        pred_game = pred['predicted_game']
        suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
        trig = SUIT_DISPLAY.get(pred['triggered_by'], pred['triggered_by'])
        time_str = pred['predicted_at'].strftime('%H:%M:%S')
        source = pred.get('source', '')

        status = pred['status']
        if status == 'en_cours':
            status_str = "⏳ En cours..."
        elif status == 'gagne':
            status_str = "✅ GAGNÉ"
        elif status == 'perdu':
            status_str = "❌ PERDU"
        else:
            status_str = f"✅ {status}"

        lines.append(
            f"{i}. 🕐 `{time_str}` | **Game #N{pred_game}** {suit} [{source}]\n"
            f"   📉 Déclenché par: {trig} absent\n"
            f"   📊 Résultat: {status_str}"
        )
        lines.append("")

    if pending_predictions:
        lines.append("**🔮 PRÉDICTIONS ACTIVES:**")
        for num, pred in sorted(pending_predictions.items()):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            ar = pred.get('awaiting_rattrapage', 0)
            src = pred.get('source', '')
            st = f"Attente R{ar} (#{num + ar})" if ar > 0 else "Vérification directe"
            lines.append(f"• Game #N{num} {suit} [{src}]: {st}")
        lines.append("")

    lines.append("═══════════════════════════════════════")
    await event.respond("\n".join(lines))

async def check_channel_access(channel_id) -> dict:
    """Vérifie si le bot peut accéder et écrire dans un canal."""
    result = {'id': channel_id, 'status': '❌', 'name': 'Inaccessible', 'can_write': False, 'error': ''}
    if not channel_id:
        result['error'] = 'ID non configuré'
        return result
    try:
        entity = await resolve_channel(channel_id)
        if not entity:
            result['error'] = 'Canal introuvable'
            return result
        result['name'] = getattr(entity, 'title', 'Sans titre')
        # Vérifier les permissions d'écriture
        try:
            from telethon.tl.functions.channels import GetParticipantRequest
            from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
            me = await client.get_me()
            participant = await client(GetParticipantRequest(entity, me))
            p = participant.participant
            if isinstance(p, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                result['status'] = '✅ Admin'
                result['can_write'] = True
            else:
                result['status'] = '⚠️ Membre'
                result['can_write'] = False
                result['error'] = 'Bot non admin — ne peut pas publier'
        except Exception:
            # Si on ne peut pas lire le participant, on teste en résolvant seulement
            result['status'] = '⚠️ Accessible'
            result['error'] = 'Permissions non vérifiées'
    except Exception as e:
        err = str(e)
        if 'Could not find' in err or 'PeerChannel' in err:
            result['error'] = 'Bot non membre du canal'
        else:
            result['error'] = err[:50]
    return result


async def cmd_channels(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🔍 Vérification de tous les canaux en cours...")

    canaux = [
        ("📢 Double Canal (escalade)", DOUBLE_CANAL_CHANNEL_ID),
        ("🔕 C1 Silencieux", C1_SILENT_CHANNEL_ID),
        ("🔕 C2 Silencieux", C2_SILENT_CHANNEL_ID),
        ("🔕 C3 Silencieux", C3_SILENT_CHANNEL_ID),
        ("🎮 Canal principal", PREDICTION_CHANNEL_ID),
    ]

    lines = ["📡 **ÉTAT DES CANAUX**", "═══════════════════════════════════════", ""]

    all_ok = True
    for label, cid in canaux:
        info = await check_channel_access(cid)
        if not info['can_write']:
            all_ok = False
        name_str = f"**{info['name']}**" if info['name'] != 'Inaccessible' else "_Inaccessible_"
        err_str = f"\n     ⚠️ {info['error']}" if info['error'] else ""
        lines.append(
            f"{label}\n"
            f"     {info['status']} | ID: `{cid}`\n"
            f"     {name_str}{err_str}"
        )
        lines.append("")

    lines.append("═══════════════════════════════════════")
    if all_ok:
        lines.append("✅ **Tous les canaux sont accessibles et configurés correctement.**")
    else:
        lines.append("❌ **Certains canaux nécessitent une action.**")
        lines.append("")
        lines.append("Pour chaque canal ❌ :")
        lines.append("1. Ouvrez le canal dans Telegram")
        lines.append("2. Allez dans **Administrateurs**")
        lines.append("3. Ajoutez le bot avec la permission **Publier des messages**")

    lines.append(f"\n📊 **Config:** API poll={API_POLL_INTERVAL}s | Jeu actuel: #{current_game_number}")
    lines.append(f"**Compteurs:** C1 B={C1_B} | C2 B={C2_B} | C3 B={C3_B}")

    await event.respond("\n".join(lines))

async def cmd_test(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🧪 Test de connexion au canal de prédiction...")

    try:
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ PREDICTION_CHANNEL_ID non configuré")
            return

        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond(
                f"❌ **Canal inaccessible** `{PREDICTION_CHANNEL_ID}`\n\n"
                f"Vérifiez:\n"
                f"1. L'ID est correct\n"
                f"2. Le bot est administrateur du canal\n"
                f"3. Le bot a les permissions d'envoi"
            )
            return

        test_msg = (
            f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
            f"🎮GAME: #N9999\n"
            f"🃏Carte ♠️:⌛\n"
            f"Mode: Dogon 2\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')} [TEST]"
        )
        sent = await client.send_message(prediction_entity, test_msg)
        await asyncio.sleep(2)

        await client.edit_message(
            prediction_entity, sent.id,
            f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
            f"🎮GAME: #N9999\n"
            f"🃏Carte ♠️:✅\n"
            f"Mode: Dogon 2\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')} [TEST]"
        )
        await asyncio.sleep(2)
        await client.delete_messages(prediction_entity, [sent.id])

        pred_name_display = getattr(prediction_entity, 'title', str(prediction_entity.id))
        await event.respond(
            f"✅ **TEST RÉUSSI!**\n\n"
            f"Canal: `{pred_name_display}`\n"
            f"Envoi, modification et suppression: OK"
        )

    except ChatWriteForbiddenError:
        await event.respond("❌ **Permission refusée** — Ajoutez le bot comme administrateur.")
    except Exception as e:
        await event.respond(f"❌ Échec du test: {e}")

async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel admin")
    await event.respond("✅ Reset effectué! Compteurs remis à zéro.")

async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    lines = [
        "📈 **ÉTAT DU BOT**",
        "",
        get_c1_status_text(),
        "",
        get_c2_status_text(),
        "",
        f"🔮 Prédictions canal actives: {len(pending_predictions)}",
        f"📡 Source: API 1xBet (polling {API_POLL_INTERVAL}s)",
        f"📦 Jeux en cache: {len(api_results_cache)}",
        f"🔄 Reset automatique: partie #1440 terminée",
    ]

    if pending_predictions:
        lines.append("")
        for num, pred in sorted(pending_predictions.items()):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            trig = SUIT_DISPLAY.get(pred['triggered_by'], pred['triggered_by'])
            src = pred.get('source', '')
            ar = pred.get('awaiting_rattrapage', 0)
            st = f"R{ar} en attente (#{num+ar})" if ar > 0 else "Vérification directe"
            lines.append(f"• Game #N{num} {suit} [{src}] (déclenché par {trig}): {st}")

    await event.respond("\n".join(lines))

async def cmd_announce(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.split(' ', 1)
    if len(parts) < 2:
        await event.respond("Usage: `/announce Message`")
        return

    text = parts[1].strip()
    if len(text) > 500:
        await event.respond("❌ Trop long (max 500 caractères)")
        return

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond("❌ Canal de prédiction non accessible")
            return

        now = datetime.now()
        msg = (
            f"╔══════════════════════════════════════╗\n"
            f"║     📢 ANNONCE OFFICIELLE 📢          ║\n"
            f"╠══════════════════════════════════════╣\n\n"
            f"{text}\n\n"
            f"╠══════════════════════════════════════╣\n"
            f"║  📅 {now.strftime('%d/%m/%Y')}  🕐 {now.strftime('%H:%M')}\n"
            f"╚══════════════════════════════════════╝\n\n"
            f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨"
        )
        sent = await client.send_message(prediction_entity, msg)
        await event.respond(f"✅ Annonce envoyée (ID: {sent.id})")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

async def cmd_predi(event):
    global prediction_intervals, intervals_enabled

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    raw = event.message.message.strip()

    add_match = re.match(r'^/predi\+(\d{1,2})-(\d{1,2})$', raw)
    if add_match:
        start_h = int(add_match.group(1))
        end_h = int(add_match.group(2))
        if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
            await event.respond("❌ Heures invalides. Utilisez des valeurs entre 0 et 23.")
            return
        if start_h == end_h:
            await event.respond("❌ L'heure de début et de fin ne peuvent pas être identiques.")
            return
        for iv in prediction_intervals:
            if iv["start"] == start_h and iv["end"] == end_h:
                await event.respond(f"⚠️ L'intervalle {start_h:02d}h00→{end_h:02d}h00 existe déjà.")
                return
        prediction_intervals.append({"start": start_h, "end": end_h})
        await event.respond(
            f"✅ Intervalle ajouté: {start_h:02d}h00 → {end_h:02d}h00 (heure Bénin)\n\n"
            + get_intervals_status_text()
        )
        return

    parts = raw.split()

    if len(parts) == 1:
        await event.respond(
            get_intervals_status_text() + "\n\n"
            "**Commandes:**\n"
            "`/predi+HH-HH` — Ajouter un intervalle (ex: `/predi+12-15`)\n"
            "`/predi del <N>` — Supprimer l'intervalle N\n"
            "`/predi clear` — Supprimer tous les intervalles\n"
            "`/predi on` — Activer la restriction\n"
            "`/predi off` — Désactiver la restriction"
        )
        return

    arg = parts[1].lower()

    if arg == "on":
        intervals_enabled = True
        await event.respond("✅ **Restriction horaire ACTIVÉE**\n\n" + get_intervals_status_text())

    elif arg == "off":
        intervals_enabled = False
        await event.respond("❌ **Restriction horaire DÉSACTIVÉE**\n\n" + get_intervals_status_text())

    elif arg == "clear":
        prediction_intervals = []
        await event.respond("🗑️ Tous les intervalles supprimés.\n\n" + get_intervals_status_text())

    elif arg == "del":
        if len(parts) < 3:
            await event.respond("Usage: `/predi del <N>`")
            return
        try:
            idx = int(parts[2]) - 1
            if not (0 <= idx < len(prediction_intervals)):
                await event.respond(f"❌ Index invalide. Il y a {len(prediction_intervals)} intervalle(s).")
                return
            removed = prediction_intervals.pop(idx)
            await event.respond(
                f"🗑️ Intervalle {removed['start']:02d}h00→{removed['end']:02d}h00 supprimé.\n\n"
                + get_intervals_status_text()
            )
        except ValueError:
            await event.respond("❌ Numéro invalide.")
    else:
        await event.respond(
            "⏰ **INTERVALLES - Aide**\n\n"
            "`/predi` — Afficher l'état\n"
            "`/predi+HH-HH` — Ajouter un intervalle (ex: `/predi+12-15`)\n"
            "`/predi del <N>` — Supprimer l'intervalle N\n"
            "`/predi clear` — Supprimer tous les intervalles\n"
            "`/predi on` — Activer la restriction horaire\n"
            "`/predi off` — Désactiver la restriction horaire"
        )

async def cmd_start(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(
        "🎰 **Bienvenue sur BACCARAT PRO ✨**\n\n"
        "Bot de prédiction Baccarat intelligent.\n\n"
        "📊 **Compteur1** (B=5) :\n"
        "• ♣️ absent 5x → silencieux ♦️\n"
        "• ♦️ absent 5x → silencieux ♣️\n"
        "• ♠️ absent 5x → silencieux ❤️\n"
        "• ❤️ absent 5x → silencieux ♠️\n"
        "• Après 2 pertes silencieuses → envoi au canal\n\n"
        "📊 **Compteur2** (B=8) :\n"
        "• ❤️ absent 8x → silencieux ♣️\n"
        "• ♣️ absent 8x → silencieux ❤️\n"
        "• ♠️ absent 8x → silencieux ♦️\n"
        "• ♦️ absent 8x → silencieux ♠️\n"
        "• Après 1 perte silencieuse → envoi au canal\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📖 Tapez /help pour toutes les commandes.\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(
        "📖 **BACCARAT PRO ✨ - AIDE**\n\n"
        "**📊 Compteur1 (B=5) — Dogon 2:**\n"
        "• Compte les absences consécutives du joueur\n"
        "• Seuil B=5 → prédiction silencieuse\n"
        "• Après 2 pertes silencieuses consécutives → envoi au canal\n"
        "• ♣️→♦️ | ♦️→♣️ | ♠️→❤️ | ❤️→♠️\n\n"
        "**📊 Compteur2 (B=8) — Dogon 2:**\n"
        "• Compte les absences consécutives du joueur\n"
        "• Seuil B=8 → prédiction silencieuse\n"
        "• Après 1 perte silencieuse → envoi au canal\n"
        "• ❤️→♣️ | ♣️→❤️ | ♠️→♦️ | ♦️→♠️\n\n"
        "**🔧 Commandes Admin:**\n"
        "`/compteur1` — État et gestion du Compteur1\n"
        "`/compteur1 on/off` — Activer/désactiver\n"
        "`/compteur1 reset` — Remettre à zéro\n"
        "`/compteur2` — État et gestion du Compteur2\n"
        "`/compteur2 on/off` — Activer/désactiver\n"
        "`/compteur2 reset` — Remettre à zéro\n"
        "`/compteur3` — État du Compteur3 (B=5, double canal après 2 pertes)\n"
        "`/compteur3 on/off` — Activer/désactiver\n"
        "`/compteur3 reset` — Remettre à zéro\n"
        "`/predi` — Gérer les intervalles horaires\n"
        "`/predi+HH-HH` — Ajouter un intervalle\n"
        "`/silencieux` — Historique des prédictions silencieuses\n"
        "`/silencieux all` — Tout l'historique silencieux\n"
        "`/status` — État complet\n"
        "`/history` — Historique des prédictions canal\n"
        "`/channels` — Configuration\n"
        "`/test` — Tester le canal\n"
        "`/reset` — Reset complet\n"
        "`/announce <msg>` — Annonce\n"
        "`/help` — Cette aide"
    )

# ============================================================================
# CONFIGURATION DES HANDLERS
# ============================================================================

def setup_handlers():
    client.add_event_handler(cmd_compteur1, events.NewMessage(pattern=r'^/compteur1'))
    client.add_event_handler(cmd_compteur2, events.NewMessage(pattern=r'^/compteur2'))
    client.add_event_handler(cmd_compteur3, events.NewMessage(pattern=r'^/compteur3'))
    client.add_event_handler(cmd_silencieux, events.NewMessage(pattern=r'^/silencieux'))
    client.add_event_handler(cmd_attente, events.NewMessage(pattern=r'^/attente'))
    client.add_event_handler(cmd_predi, events.NewMessage(pattern=r'^/predi'))
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history, events.NewMessage(pattern=r'^/history$'))
    client.add_event_handler(cmd_start, events.NewMessage(pattern=r'^/start$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_channels, events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_test, events.NewMessage(pattern=r'^/test$'))
    client.add_event_handler(cmd_announce, events.NewMessage(pattern=r'^/announce'))

# ============================================================================
# DÉMARRAGE
# ============================================================================

async def start_bot():
    global client, prediction_channel_ok

    client = TelegramClient(StringSession(TELEGRAM_SESSION), API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()

        if PREDICTION_CHANNEL_ID:
            try:
                pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
                if pred_entity:
                    prediction_channel_ok = True
                    logger.info(f"✅ Canal prédiction OK: {getattr(pred_entity, 'title', 'Unknown')}")
                else:
                    logger.error(f"❌ Canal prédiction inaccessible: {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur vérification canal: {e}")

        logger.info(
            f"🤖 BACCARAT PRO ✨ démarré | "
            f"C1 B={C1_B} | C2 B={C2_B} | C3 B={C3_B}"
        )
        logger.info(f"🔄 Reset automatique configuré: fin de la partie #1440")
        return True

    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    try:
        if not await start_bot():
            return

        asyncio.create_task(api_polling_loop())
        logger.info("🔄 Polling API dynamique démarré")

        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        app.router.add_get('/', lambda r: web.Response(text="BACCARAT PRO ✨ Running"))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"🌐 Health check sur port {PORT}")

        await client.run_until_disconnected()

    except KeyboardInterrupt:
        logger.info("🛑 Arrêt demandé")
    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
        logger.error(traceback.format_exc())

if __name__ == '__main__':
    asyncio.run(main())
