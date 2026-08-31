//+------------------------------------------------------------------+
//| CopyTrader_Pendings.mqh                                         |
//| Pending tracking + PENDING_OPEN/MODIFY/CLOSE + snapshot recovery|
//+------------------------------------------------------------------+
#ifndef __COPYTRADER_PENDINGS_MQH__
#define __COPYTRADER_PENDINGS_MQH__

// Requires CopyTrader_State.mqh included BEFORE this file:
// PendingInfo g_lastPendings[]; int g_lastPendingCount;
// long g_sentPendingTickets[]; int g_sentPendingCount;
//
// Also requires these helpers somewhere in your project:
// - string JsonEscape(const string s);
// - void SendToServer(const string json);
// - bool GetSymbolTradeMeta(const string symbol,
//                           double &contract_size,
//                           double &vol_min,
//                           double &vol_max,
//                           double &vol_step,
//                           double &tick_size,
//                           double &tick_value,
//                           double &point,
//                           int &digits);
// - extern string MagicNumberFilter;

bool PendingAlreadySent(const long ticket)
{
   for(int i = 0; i < g_sentPendingCount; i++)
   {
      if(g_sentPendingTickets[i] == ticket)
         return true;
   }

   return false;
}

void MarkPendingSent(const long ticket)
{
   if(PendingAlreadySent(ticket))
      return;

   ArrayResize(g_sentPendingTickets, g_sentPendingCount + 1);
   g_sentPendingTickets[g_sentPendingCount] = ticket;
   g_sentPendingCount++;
}

bool IsPendingOrderType(const int ord_type)
{
   return (
      ord_type == ORDER_TYPE_BUY_LIMIT ||
      ord_type == ORDER_TYPE_SELL_LIMIT ||
      ord_type == ORDER_TYPE_BUY_STOP ||
      ord_type == ORDER_TYPE_SELL_STOP ||
      ord_type == ORDER_TYPE_BUY_STOP_LIMIT ||
      ord_type == ORDER_TYPE_SELL_STOP_LIMIT
   );
}

//======================================================
// CLOSE de-dupe: TT delete vs polling removal
//======================================================
#define CLOSE_DEDUPE_WINDOW_MS 3000

struct RecentClose
{
   long ticket;
   long ts_ms;
};

static RecentClose g_recentClose[];
static int g_recentCloseCount = 0;

long NowMs()
{
   return (long)TimeLocal() * 1000;
}

void RememberClosedTicket(const long ticket)
{
   long now = NowMs();

   for(int i = g_recentCloseCount - 1; i >= 0; i--)
   {
      if(now - g_recentClose[i].ts_ms > CLOSE_DEDUPE_WINDOW_MS)
      {
         for(int k = i; k < g_recentCloseCount - 1; k++)
            g_recentClose[k] = g_recentClose[k + 1];

         g_recentCloseCount--;
         ArrayResize(g_recentClose, g_recentCloseCount);
         continue;
      }

      if(g_recentClose[i].ticket == ticket)
      {
         g_recentClose[i].ts_ms = now;
         return;
      }
   }

   ArrayResize(g_recentClose, g_recentCloseCount + 1);
   g_recentClose[g_recentCloseCount].ticket = ticket;
   g_recentClose[g_recentCloseCount].ts_ms = now;
   g_recentCloseCount++;
}

bool WasRecentlyClosed(const long ticket)
{
   long now = NowMs();

   for(int i = g_recentCloseCount - 1; i >= 0; i--)
   {
      if(now - g_recentClose[i].ts_ms > CLOSE_DEDUPE_WINDOW_MS)
      {
         for(int k = i; k < g_recentCloseCount - 1; k++)
            g_recentClose[k] = g_recentClose[k + 1];

         g_recentCloseCount--;
         ArrayResize(g_recentClose, g_recentCloseCount);
         continue;
      }

      if(g_recentClose[i].ticket == ticket)
         return true;
   }

   return false;
}

//======================================================
// Pending-order snapshot store
//======================================================
struct PendingSnap
{
   long ticket;
   string symbol;
   int type;
   double volume;
   double price_open;
   double price_stoplimit;
   double stopLoss;
   double takeProfit;
   long magicNumber;
   datetime expiration;
};

static PendingSnap g_pendSnap[];
static int g_pendSnapCount = 0;

int FindPendSnapIndex(const long ticket)
{
   for(int i = 0; i < g_pendSnapCount; i++)
   {
      if(g_pendSnap[i].ticket == ticket)
         return i;
   }

   return -1;
}

void RemovePendSnap(const long ticket)
{
   int idx = FindPendSnapIndex(ticket);
   if(idx < 0)
      return;

   for(int i = idx; i < g_pendSnapCount - 1; i++)
      g_pendSnap[i] = g_pendSnap[i + 1];

   g_pendSnapCount--;
   ArrayResize(g_pendSnap, g_pendSnapCount);
}

bool IsSelectedPendingAllowed()
{
   int ord_type = (int)OrderGetInteger(ORDER_TYPE);
   if(!IsPendingOrderType(ord_type))
      return false;

   long magic = (long)OrderGetInteger(ORDER_MAGIC);

   // Note: this supports ONE exact magic number only.
   // Leave MagicNumberFilter empty to allow all pending-order magics.
   if(MagicNumberFilter != "" &&
      magic != StringToInteger(MagicNumberFilter))
   {
      return false;
   }

   return true;
}

bool UpsertPendSnap_FromLiveOrder(const ulong ticket_u)
{
   if(ticket_u == 0)
      return false;

   if(!OrderSelect(ticket_u))
      return false;

   if(!IsSelectedPendingAllowed())
      return false;

   long ticket = (long)ticket_u;
   int idx = FindPendSnapIndex(ticket);

   if(idx < 0)
   {
      ArrayResize(g_pendSnap, g_pendSnapCount + 1);
      idx = g_pendSnapCount++;
   }

   g_pendSnap[idx].ticket = ticket;
   g_pendSnap[idx].symbol = OrderGetString(ORDER_SYMBOL);
   g_pendSnap[idx].type = (int)OrderGetInteger(ORDER_TYPE);
   g_pendSnap[idx].volume = OrderGetDouble(ORDER_VOLUME_CURRENT);
   g_pendSnap[idx].price_open = OrderGetDouble(ORDER_PRICE_OPEN);
   g_pendSnap[idx].price_stoplimit = OrderGetDouble(ORDER_PRICE_STOPLIMIT);
   g_pendSnap[idx].stopLoss = OrderGetDouble(ORDER_SL);
   g_pendSnap[idx].takeProfit = OrderGetDouble(ORDER_TP);
   g_pendSnap[idx].magicNumber = (long)OrderGetInteger(ORDER_MAGIC);
   g_pendSnap[idx].expiration = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);

   return true;
}

//======================================================
// Snapshot current pending orders into legacy state list
//======================================================
void UpdatePendingList()
{
   int totalOrders = OrdersTotal();
   ArrayResize(g_lastPendings, totalOrders);

   int idx = 0;

   for(int i = 0; i < totalOrders; i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;

      if(!OrderSelect(ticket))
         continue;

      if(!IsSelectedPendingAllowed())
         continue;

      g_lastPendings[idx].ticket = (long)ticket;
      g_lastPendings[idx].symbol = OrderGetString(ORDER_SYMBOL);
      g_lastPendings[idx].type = (int)OrderGetInteger(ORDER_TYPE);
      g_lastPendings[idx].volume = OrderGetDouble(ORDER_VOLUME_CURRENT);
      g_lastPendings[idx].price_open = OrderGetDouble(ORDER_PRICE_OPEN);
      g_lastPendings[idx].price_stoplimit = OrderGetDouble(ORDER_PRICE_STOPLIMIT);
      g_lastPendings[idx].stopLoss = OrderGetDouble(ORDER_SL);
      g_lastPendings[idx].takeProfit = OrderGetDouble(ORDER_TP);
      g_lastPendings[idx].magicNumber = (long)OrderGetInteger(ORDER_MAGIC);
      g_lastPendings[idx].expiration =
         (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);

      idx++;
   }

   g_lastPendingCount = idx;
   ArrayResize(g_lastPendings, g_lastPendingCount);
}

//======================================================
// Helpers
//======================================================
string PendingTypeToSide(const int ord_type)
{
   if(
      ord_type == ORDER_TYPE_SELL_LIMIT ||
      ord_type == ORDER_TYPE_SELL_STOP ||
      ord_type == ORDER_TYPE_SELL_STOP_LIMIT
   )
   {
      return "SELL";
   }

   return "BUY";
}

string PendingTypeToName(const int ord_type)
{
   if(
      ord_type == ORDER_TYPE_BUY_LIMIT ||
      ord_type == ORDER_TYPE_SELL_LIMIT
   )
   {
      return "limit";
   }

   if(
      ord_type == ORDER_TYPE_BUY_STOP ||
      ord_type == ORDER_TYPE_SELL_STOP
   )
   {
      return "stop";
   }

   return "stop_limit";
}

bool PendingFieldsChanged(
   const PendingSnap &a,
   const PendingSnap &b
)
{
   return (
      a.type != b.type ||
      a.volume != b.volume ||
      a.price_open != b.price_open ||
      a.price_stoplimit != b.price_stoplimit ||
      a.stopLoss != b.stopLoss ||
      a.takeProfit != b.takeProfit ||
      a.expiration != b.expiration ||
      a.magicNumber != b.magicNumber ||
      a.symbol != b.symbol
   );
}

//======================================================
// JSON builders
//======================================================
void SendPendingOpenSignalEx(
   const ulong ticket,
   const string event_type,
   const string snapshot_reason = ""
)
{
   if(!OrderSelect(ticket))
   {
      Print(
         "SendPendingOpenSignalEx: OrderSelect failed for ",
         ticket,
         " err=",
         GetLastError()
      );
      return;
   }

   if(!IsSelectedPendingAllowed())
   {
      PrintFormat(
         "Pending signal skipped by filter | ticket=%I64u symbol=%s magic=%I64d filter=%s",
         ticket,
         OrderGetString(ORDER_SYMBOL),
         (long)OrderGetInteger(ORDER_MAGIC),
         MagicNumberFilter
      );
      return;
   }

   string symbol = OrderGetString(ORDER_SYMBOL);
   int ord_type = (int)OrderGetInteger(ORDER_TYPE);
   long magic = (long)OrderGetInteger(ORDER_MAGIC);
   double volume = OrderGetDouble(ORDER_VOLUME_CURRENT);
   double price_open = OrderGetDouble(ORDER_PRICE_OPEN);
   double price_stoplimit = OrderGetDouble(ORDER_PRICE_STOPLIMIT);
   double sl = OrderGetDouble(ORDER_SL);
   double tp = OrderGetDouble(ORDER_TP);
   datetime exp = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);

   double contract_size = 0.0;
   double vol_min = 0.0;
   double vol_max = 0.0;
   double vol_step = 0.0;
   double tick_size = 0.0;
   double tick_value = 0.0;
   double point = 0.0;
   int digits = 0;

   GetSymbolTradeMeta(
      symbol,
      contract_size,
      vol_min,
      vol_max,
      vol_step,
      tick_size,
      tick_value,
      point,
      digits
   );

   string side = PendingTypeToSide(ord_type);
   string pending_type = PendingTypeToName(ord_type);

   long exp_ms = 0;
   if(exp > 0)
      exp_ms = (long)exp * 1000;

   int priceDigits = (digits > 0 ? digits : 5);

   bool is_snapshot = (
      event_type == "PENDING_SNAPSHOT" ||
      snapshot_reason == "startup" ||
      snapshot_reason == "recovery" ||
      snapshot_reason == "startup_sync"
   );

   string json = "{";
   json += "\"event_type\":\"" + JsonEscape(event_type) + "\",";
   json += "\"ticket\":" + (string)ticket + ",";
   json += "\"symbol\":\"" + JsonEscape(symbol) + "\",";
   json += "\"side\":\"" + side + "\",";
   json += "\"volume\":" + DoubleToString(volume, 2) + ",";
   json += "\"pending_type\":\"" + pending_type + "\",";

   if(pending_type == "limit")
   {
      json += "\"limit_price\":" +
              DoubleToString(price_open, priceDigits) + ",";
   }
   else if(pending_type == "stop")
   {
      json += "\"stop_price\":" +
              DoubleToString(price_open, priceDigits) + ",";
   }
   else
   {
      json += "\"stop_price\":" +
              DoubleToString(price_open, priceDigits) + ",";
      json += "\"limit_price\":" +
              DoubleToString(price_stoplimit, priceDigits) + ",";
   }

   json += "\"sl\":" + DoubleToString(sl, priceDigits) + ",";
   json += "\"tp\":" + DoubleToString(tp, priceDigits) + ",";
   json += "\"expiration_ms\":" + (string)exp_ms + ",";
   json += "\"magic\":" + (string)magic + ",";
   json += "\"mt5_contract_size\":" +
           DoubleToString(contract_size, 8) + ",";
   json += "\"mt5_volume_min\":" +
           DoubleToString(vol_min, 8) + ",";
   json += "\"mt5_volume_max\":" +
           DoubleToString(vol_max, 8) + ",";
   json += "\"mt5_volume_step\":" +
           DoubleToString(vol_step, 8) + ",";
   json += "\"mt5_tick_size\":" +
           DoubleToString(tick_size, 10) + ",";
   json += "\"mt5_tick_value\":" +
           DoubleToString(tick_value, 10) + ",";
   json += "\"point\":" + DoubleToString(point, 10) + ",";
   json += "\"digits\":" + IntegerToString(digits);

   if(snapshot_reason != "")
      json += ",\"snapshot_reason\":\"" +
              JsonEscape(snapshot_reason) + "\"";

   if(is_snapshot)
   {
      json += ",\"startupSync\":true";
      json += ",\"startupRecovery\":true";
      json += ",\"syncOrigin\":\"startup\"";
      json += ",\"recovery\":true";
   }

   json += "}";

   PrintFormat(
      "PENDING SIGNAL SEND | event=%s ticket=%I64u symbol=%s side=%s type=%s magic=%I64d volume=%.2f",
      event_type,
      ticket,
      symbol,
      side,
      pending_type,
      magic,
      volume
   );

   SendToServer(json);
}

void SendPendingOpenSignal(const ulong ticket)
{
   SendPendingOpenSignalEx(ticket, "PENDING_OPEN");
}

void SendPendingSnapshotSignal(
   const ulong ticket,
   const string snapshot_reason = "periodic"
)
{
   SendPendingOpenSignalEx(
      ticket,
      "PENDING_SNAPSHOT",
      snapshot_reason
   );
}

void SendPendingModifySignal(const PendingSnap &snap)
{
   string side = PendingTypeToSide(snap.type);
   string pending_type = PendingTypeToName(snap.type);

   long exp_ms = 0;
   if(snap.expiration > 0)
      exp_ms = (long)snap.expiration * 1000;

   double contract_size = 0.0;
   double vol_min = 0.0;
   double vol_max = 0.0;
   double vol_step = 0.0;
   double tick_size = 0.0;
   double tick_value = 0.0;
   double point = 0.0;
   int digits = 0;

   GetSymbolTradeMeta(
      snap.symbol,
      contract_size,
      vol_min,
      vol_max,
      vol_step,
      tick_size,
      tick_value,
      point,
      digits
   );

   int priceDigits = (digits > 0 ? digits : 5);

   string json = "{";
   json += "\"event_type\":\"PENDING_MODIFY\",";
   json += "\"ticket\":" + (string)snap.ticket + ",";
   json += "\"symbol\":\"" + JsonEscape(snap.symbol) + "\",";
   json += "\"side\":\"" + side + "\",";
   json += "\"volume\":" + DoubleToString(snap.volume, 2) + ",";
   json += "\"pending_type\":\"" + pending_type + "\",";

   if(pending_type == "limit")
   {
      json += "\"limit_price\":" +
              DoubleToString(snap.price_open, priceDigits) + ",";
   }
   else if(pending_type == "stop")
   {
      json += "\"stop_price\":" +
              DoubleToString(snap.price_open, priceDigits) + ",";
   }
   else
   {
      json += "\"stop_price\":" +
              DoubleToString(snap.price_open, priceDigits) + ",";
      json += "\"limit_price\":" +
              DoubleToString(snap.price_stoplimit, priceDigits) + ",";
   }

   json += "\"sl\":" + DoubleToString(snap.stopLoss, priceDigits) + ",";
   json += "\"tp\":" + DoubleToString(snap.takeProfit, priceDigits) + ",";
   json += "\"expiration_ms\":" + (string)exp_ms + ",";
   json += "\"magic\":" + (string)snap.magicNumber + ",";
   json += "\"mt5_contract_size\":" +
           DoubleToString(contract_size, 8) + ",";
   json += "\"mt5_volume_min\":" +
           DoubleToString(vol_min, 8) + ",";
   json += "\"mt5_volume_max\":" +
           DoubleToString(vol_max, 8) + ",";
   json += "\"mt5_volume_step\":" +
           DoubleToString(vol_step, 8) + ",";
   json += "\"mt5_tick_size\":" +
           DoubleToString(tick_size, 10) + ",";
   json += "\"mt5_tick_value\":" +
           DoubleToString(tick_value, 10) + ",";
   json += "\"point\":" + DoubleToString(point, 10) + ",";
   json += "\"digits\":" + IntegerToString(digits);
   json += "}";

   PrintFormat(
      "PENDING MODIFY SEND | ticket=%I64d symbol=%s magic=%I64d",
      snap.ticket,
      snap.symbol,
      snap.magicNumber
   );

   SendToServer(json);
}

void SendPendingCloseSignal(
   const long ticket,
   const string symbol,
   const long magic
)
{
   string json = "{";
   json += "\"event_type\":\"PENDING_CLOSE\",";
   json += "\"ticket\":" + (string)ticket + ",";
   json += "\"magic\":" + (string)magic;

   if(symbol != "")
      json += ",\"symbol\":\"" + JsonEscape(symbol) + "\"";

   json += "}";

   PrintFormat(
      "PENDING CLOSE SEND | ticket=%I64d symbol=%s magic=%I64d",
      ticket,
      symbol,
      magic
   );

   SendToServer(json);
}

//======================================================
// Explicit startup / periodic pending snapshot sender
//
// This intentionally bypasses PendingAlreadySent(). The EA sends the
// current active MT5 pending order state again, allowing a restarted
// Python bridge to recover. Python MUST make PENDING_SNAPSHOT
// idempotent by checking the cTrader order label MT5_<ticket>.
//======================================================
int StartupSyncPendingOrders(
   const string snapshot_reason = "startup"
)
{
   int totalOrders = OrdersTotal();
   int sentCount = 0;
   int skippedInvalidCount = 0;
   int skippedFilterCount = 0;
   int orderSelectFailCount = 0;

   PrintFormat(
      "Pending snapshot started | reason=%s | active_orders=%d | magic_filter=%s",
      snapshot_reason,
      totalOrders,
      (MagicNumberFilter == "" ? "<ALL>" : MagicNumberFilter)
   );

   for(int i = 0; i < totalOrders; i++)
   {
      ulong ticket = OrderGetTicket(i);

      if(ticket == 0)
      {
         skippedInvalidCount++;
         PrintFormat(
            "PENDING SNAPSHOT SKIP | reason=zero_ticket | order_index=%d",
            i
         );
         continue;
      }

      if(!OrderSelect(ticket))
      {
         orderSelectFailCount++;
         PrintFormat(
            "PENDING SNAPSHOT SKIP | reason=order_select_failed | ticket=%I64u err=%d",
            ticket,
            GetLastError()
         );
         continue;
      }

      string symbol = OrderGetString(ORDER_SYMBOL);
      int orderType = (int)OrderGetInteger(ORDER_TYPE);
      long magic = (long)OrderGetInteger(ORDER_MAGIC);
      double volume = OrderGetDouble(ORDER_VOLUME_CURRENT);

      if(!IsPendingOrderType(orderType))
      {
         skippedInvalidCount++;

         PrintFormat(
            "PENDING SNAPSHOT SKIP | reason=not_pending_type | ticket=%I64u symbol=%s type=%d magic=%I64d",
            ticket,
            symbol,
            orderType,
            magic
         );
         continue;
      }

      if(!IsSelectedPendingAllowed())
      {
         skippedFilterCount++;

         PrintFormat(
            "PENDING SNAPSHOT SKIP | reason=magic_filter | ticket=%I64u symbol=%s type=%s magic=%I64d filter=%s",
            ticket,
            symbol,
            PendingTypeToName(orderType),
            magic,
            (MagicNumberFilter == "" ? "<ALL>" : MagicNumberFilter)
         );
         continue;
      }

      if(!UpsertPendSnap_FromLiveOrder(ticket))
      {
         PrintFormat(
            "PENDING SNAPSHOT SKIP | reason=snapshot_upsert_failed | ticket=%I64u symbol=%s magic=%I64d",
            ticket,
            symbol,
            magic
         );
         continue;
      }

      PrintFormat(
         "PENDING SNAPSHOT SEND | ticket=%I64u symbol=%s side=%s type=%s magic=%I64d volume=%.2f",
         ticket,
         symbol,
         PendingTypeToSide(orderType),
         PendingTypeToName(orderType),
         magic,
         volume
      );

      MarkPendingSent((long)ticket);
      SendPendingSnapshotSignal(ticket, snapshot_reason);
      sentCount++;
   }

   UpdatePendingList();

   PrintFormat(
      "Pending snapshot completed | reason=%s | sent=%d tracked=%d skipped_invalid=%d skipped_filter=%d order_select_failed=%d",
      snapshot_reason,
      sentCount,
      g_lastPendingCount,
      skippedInvalidCount,
      skippedFilterCount,
      orderSelectFailCount
   );

   return sentCount;
}

//======================================================
// OnTradeTransaction hook
//======================================================
void Pendings_OnTradeTransaction(const MqlTradeTransaction &trans)
{
   if(trans.order == 0)
      return;

   if(trans.type == TRADE_TRANSACTION_ORDER_ADD)
   {
      UpsertPendSnap_FromLiveOrder((ulong)trans.order);
      return;
   }

   if(trans.type == TRADE_TRANSACTION_ORDER_UPDATE)
   {
      long t = (long)trans.order;
      int oldIdx = FindPendSnapIndex(t);
      PendingSnap before;
      bool haveBefore = false;

      if(oldIdx >= 0)
      {
         before = g_pendSnap[oldIdx];
         haveBefore = true;
      }

      if(!UpsertPendSnap_FromLiveOrder((ulong)trans.order))
         return;

      int newIdx = FindPendSnapIndex(t);
      if(newIdx < 0)
         return;

      PendingSnap after = g_pendSnap[newIdx];

      if(haveBefore && PendingFieldsChanged(before, after))
      {
         PrintFormat(
            "DEBUG PENDING_MODIFY: ticket=%I64d symbol=%s sl=%.5f tp=%.5f magic=%I64d",
            after.ticket,
            after.symbol,
            after.stopLoss,
            after.takeProfit,
            after.magicNumber
         );

         SendPendingModifySignal(after);
      }

      return;
   }

   if(trans.type != TRADE_TRANSACTION_ORDER_DELETE)
      return;

   if(!IsPendingOrderType((int)trans.order_type))
      return;

   ENUM_ORDER_STATE os = (ENUM_ORDER_STATE)trans.order_state;

   if(os != ORDER_STATE_CANCELED && os != ORDER_STATE_EXPIRED)
      return;

   long t = (long)trans.order;
   string sym = trans.symbol;
   long magic = 0;
   int snapIdx = FindPendSnapIndex(t);

   if(snapIdx >= 0)
   {
      magic = g_pendSnap[snapIdx].magicNumber;

      if(sym == "")
         sym = g_pendSnap[snapIdx].symbol;
   }

   PrintFormat(
      "DEBUG PENDING_CLOSE (trans): ticket=%I64d symbol=%s order_type=%s order_state=%s price=%.5f volume=%.2f magic=%I64d",
      t,
      sym,
      EnumToString((ENUM_ORDER_TYPE)trans.order_type),
      EnumToString(os),
      trans.price,
      trans.volume,
      magic
   );

   SendPendingCloseSignal(t, sym, magic);
   RememberClosedTicket(t);
   RemovePendSnap(t);
}

//======================================================
// Detect new / modified / removed pending orders
//======================================================
void CheckPendingChanges()
{
   static long prevTickets[];
   static int prevCount = -1;

   int totalOrders = OrdersTotal();
   long currTickets[];
   int currCount = 0;

   for(int i = 0; i < totalOrders; i++)
   {
      ulong ticket_u = OrderGetTicket(i);

      if(ticket_u == 0)
         continue;

      if(!OrderSelect(ticket_u))
         continue;

      if(!IsSelectedPendingAllowed())
         continue;

      long t = (long)ticket_u;
      PendingSnap before;
      bool haveBefore = false;
      int oldIdx = FindPendSnapIndex(t);

      if(oldIdx >= 0)
      {
         before = g_pendSnap[oldIdx];
         haveBefore = true;
      }

      if(!UpsertPendSnap_FromLiveOrder(ticket_u))
         continue;

      int newIdx = FindPendSnapIndex(t);
      if(newIdx < 0)
         continue;

      PendingSnap after = g_pendSnap[newIdx];

      ArrayResize(currTickets, currCount + 1);
      currTickets[currCount] = t;
      currCount++;

      if(!PendingAlreadySent(t))
      {
         SendPendingOpenSignal(ticket_u);
         MarkPendingSent(t);
      }
      else if(haveBefore && PendingFieldsChanged(before, after))
      {
         PrintFormat(
            "DEBUG PENDING_MODIFY (polling): ticket=%I64d symbol=%s sl=%.5f tp=%.5f magic=%I64d",
            after.ticket,
            after.symbol,
            after.stopLoss,
            after.takeProfit,
            after.magicNumber
         );

         SendPendingModifySignal(after);
      }
   }

   if(prevCount < 0)
   {
      ArrayFree(prevTickets);
      ArrayCopy(prevTickets, currTickets, 0, 0, WHOLE_ARRAY);
      prevCount = ArraySize(prevTickets);

      UpdatePendingList();
      return;
   }

   for(int i = 0; i < prevCount; i++)
   {
      long t = prevTickets[i];
      bool existsNow = false;

      for(int j = 0; j < currCount; j++)
      {
         if(currTickets[j] == t)
         {
            existsNow = true;
            break;
         }
      }

      if(existsNow)
         continue;

      if(WasRecentlyClosed(t))
      {
         PrintFormat(
            "DEBUG PENDING_CLOSE (polling) SKIP recent TT: ticket=%I64d",
            t
         );
      }
      else
      {
         long magic = 0;
         string symbol = "";
         int snapIdx = FindPendSnapIndex(t);

         if(snapIdx >= 0)
         {
            magic = g_pendSnap[snapIdx].magicNumber;
            symbol = g_pendSnap[snapIdx].symbol;
         }

         PrintFormat(
            "DEBUG PENDING_CLOSE (polling): ticket=%I64d magic=%I64d",
            t,
            magic
         );

         SendPendingCloseSignal(t, symbol, magic);
         RememberClosedTicket(t);
      }

      RemovePendSnap(t);
   }

   ArrayFree(prevTickets);
   ArrayCopy(prevTickets, currTickets, 0, 0, WHOLE_ARRAY);
   prevCount = ArraySize(prevTickets);

   UpdatePendingList();
}

#endif // __COPYTRADER_PENDINGS_MQH__
