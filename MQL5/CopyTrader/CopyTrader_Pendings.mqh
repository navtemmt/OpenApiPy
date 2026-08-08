//+------------------------------------------------------------------+
//| CopyTrader_Pendings.mqh                                          |
//| Pending orders tracking + PENDING_OPEN + PENDING_CLOSE (MT5 safe) |
//+------------------------------------------------------------------+
#ifndef __COPYTRADER_PENDINGS_MQH__
#define __COPYTRADER_PENDINGS_MQH__

// Requires CopyTrader_State.mqh included BEFORE this file:
// PendingInfo g_lastPendings[]; int g_lastPendingCount;
// long g_sentPendingTickets[]; int g_sentPendingCount.
//
// Also requires these helpers somewhere in your project:
// - string JsonEscape(const string s);
// - void SendToServer(const string json);
// - void GetSymbolTradeMeta(const string symbol, double &contract_size, double &vol_min, double &vol_max, double &vol_step);
// - extern string MagicNumberFilter;

bool PendingAlreadySent(const long ticket)
{
   for(int i = 0; i < g_sentPendingCount; i++)
      if(g_sentPendingTickets[i] == ticket)
         return true;
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
   return (ord_type == ORDER_TYPE_BUY_LIMIT ||
           ord_type == ORDER_TYPE_SELL_LIMIT ||
           ord_type == ORDER_TYPE_BUY_STOP ||
           ord_type == ORDER_TYPE_SELL_STOP ||
           ord_type == ORDER_TYPE_BUY_STOP_LIMIT ||
           ord_type == ORDER_TYPE_SELL_STOP_LIMIT);
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
   // stable enough for short de-dupe windows; avoids GetTickCount wrap/reset issues
   return (long)TimeLocal() * 1000;
}

void RememberClosedTicket(const long ticket)
{
   long now = NowMs();

   // prune + update existing
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
   g_recentClose[g_recentCloseCount].ts_ms  = now;
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
// Snapshot store (kept; NOT used for CLOSE anymore)
//======================================================
struct PendingSnap
{
   long     ticket;
   string   symbol;
   int      type;
   double   volume;
   double   price_open;
   double   price_stoplimit;
   double   stopLoss;
   double   takeProfit;
   long     magicNumber;
   datetime expiration;
};
static PendingSnap g_pendSnap[];
static int g_pendSnapCount = 0;

int FindPendSnapIndex(const long ticket)
{
   for(int i = 0; i < g_pendSnapCount; i++)
      if(g_pendSnap[i].ticket == ticket)
         return i;
   return -1;
}

void RemovePendSnap(const long ticket)
{
   int idx = FindPendSnapIndex(ticket);
   if(idx < 0) return;

   for(int i = idx; i < g_pendSnapCount - 1; i++)
      g_pendSnap[i] = g_pendSnap[i + 1];

   g_pendSnapCount--;
   ArrayResize(g_pendSnap, g_pendSnapCount);
}

bool UpsertPendSnap_FromLiveOrder(const ulong ticket_u)
{
   if(ticket_u == 0) return false;
   if(!OrderSelect(ticket_u)) return false;

   int ord_type = (int)OrderGetInteger(ORDER_TYPE);
   if(!IsPendingOrderType(ord_type))
      return false;

   long magic = (long)OrderGetInteger(ORDER_MAGIC);
   if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
      return false;

   long ticket = (long)ticket_u;
   int idx = FindPendSnapIndex(ticket);
   if(idx < 0)
   {
      ArrayResize(g_pendSnap, g_pendSnapCount + 1);
      idx = g_pendSnapCount++;
   }

   g_pendSnap[idx].ticket          = ticket;
   g_pendSnap[idx].symbol          = OrderGetString(ORDER_SYMBOL);
   g_pendSnap[idx].type            = ord_type;
   g_pendSnap[idx].volume          = OrderGetDouble(ORDER_VOLUME_CURRENT);
   g_pendSnap[idx].price_open      = OrderGetDouble(ORDER_PRICE_OPEN);
   g_pendSnap[idx].price_stoplimit = OrderGetDouble(ORDER_PRICE_STOPLIMIT);
   g_pendSnap[idx].stopLoss        = OrderGetDouble(ORDER_SL);
   g_pendSnap[idx].takeProfit      = OrderGetDouble(ORDER_TP);
   g_pendSnap[idx].magicNumber     = magic;
   g_pendSnap[idx].expiration      = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);
   return true;
}

//======================================================
// Snapshot current pendings (kept for last-known meta)
//======================================================
void UpdatePendingList()
{
   int totalOrders = OrdersTotal();
   ArrayResize(g_lastPendings, totalOrders);

   int idx = 0;
   for(int i = 0; i < totalOrders; i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      if(!OrderSelect(ticket)) continue;

      int ord_type = (int)OrderGetInteger(ORDER_TYPE);
      if(!IsPendingOrderType(ord_type)) continue;

      long magic = (long)OrderGetInteger(ORDER_MAGIC);
      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      string sym = OrderGetString(ORDER_SYMBOL);

      g_lastPendings[idx].ticket          = (long)ticket;
      g_lastPendings[idx].symbol          = sym;
      g_lastPendings[idx].type            = ord_type;
      g_lastPendings[idx].volume          = OrderGetDouble(ORDER_VOLUME_CURRENT);
      g_lastPendings[idx].price_open      = OrderGetDouble(ORDER_PRICE_OPEN);
      g_lastPendings[idx].price_stoplimit = OrderGetDouble(ORDER_PRICE_STOPLIMIT);
      g_lastPendings[idx].stopLoss        = OrderGetDouble(ORDER_SL);
      g_lastPendings[idx].takeProfit      = OrderGetDouble(ORDER_TP);
      g_lastPendings[idx].magicNumber     = magic;
      g_lastPendings[idx].expiration      = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);

      idx++;
   }

   g_lastPendingCount = idx;
   ArrayResize(g_lastPendings, g_lastPendingCount);
}

//======================================================
// JSON builders (NO MAGIC)
// NOTE: removed PrintFormat("DEBUG JSON -> %s", json) to avoid double logging,
// because CopyTrader_HTTP.mqh already prints JSON in SendToServer(). [page:670]
//======================================================
void SendPendingOpenSignal(const ulong ticket)
{
   if(!OrderSelect(ticket))
   {
      Print("SendPendingOpenSignal: OrderSelect failed for ", ticket, " err=", GetLastError());
      return;
   }

   string symbol = OrderGetString(ORDER_SYMBOL);
   int ord_type  = (int)OrderGetInteger(ORDER_TYPE);

   double volume          = OrderGetDouble(ORDER_VOLUME_CURRENT);
   double price_open      = OrderGetDouble(ORDER_PRICE_OPEN);
   double price_stoplimit = OrderGetDouble(ORDER_PRICE_STOPLIMIT);
   double sl              = OrderGetDouble(ORDER_SL);
   double tp              = OrderGetDouble(ORDER_TP);
   datetime exp           = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);

   double contract_size, vol_min, vol_max, vol_step;
   GetSymbolTradeMeta(symbol, contract_size, vol_min, vol_max, vol_step);

   string side = "BUY";
   string pending_type = "limit";

   if(ord_type == ORDER_TYPE_BUY_LIMIT)       { side = "BUY";  pending_type = "limit"; }
   if(ord_type == ORDER_TYPE_SELL_LIMIT)      { side = "SELL"; pending_type = "limit"; }
   if(ord_type == ORDER_TYPE_BUY_STOP)        { side = "BUY";  pending_type = "stop"; }
   if(ord_type == ORDER_TYPE_SELL_STOP)       { side = "SELL"; pending_type = "stop"; }
   if(ord_type == ORDER_TYPE_BUY_STOP_LIMIT)  { side = "BUY";  pending_type = "stop_limit"; }
   if(ord_type == ORDER_TYPE_SELL_STOP_LIMIT) { side = "SELL"; pending_type = "stop_limit"; }

   long exp_ms = 0;
   if(exp > 0) exp_ms = (long)exp * 1000;

   string json = "{";
   json += "\"event_type\":\"PENDING_OPEN\",";
   json += "\"ticket\":" + (string)ticket + ",";
   json += "\"symbol\":\"" + JsonEscape(symbol) + "\",";
   json += "\"side\":\"" + side + "\",";
   json += "\"volume\":" + DoubleToString(volume, 2) + ",";
   json += "\"pending_type\":\"" + pending_type + "\",";

   if(pending_type == "limit")
      json += "\"limit_price\":" + DoubleToString(price_open, 5) + ",";
   else if(pending_type == "stop")
      json += "\"stop_price\":" + DoubleToString(price_open, 5) + ",";
   else
   {
      json += "\"stop_price\":" + DoubleToString(price_open, 5) + ",";
      json += "\"limit_price\":" + DoubleToString(price_stoplimit, 5) + ",";
   }

   json += "\"sl\":" + DoubleToString(sl, 5) + ",";
   json += "\"tp\":" + DoubleToString(tp, 5) + ",";
   json += "\"expiration_ms\":" + (string)exp_ms + ",";
   json += "\"mt5_contract_size\":" + DoubleToString(contract_size, 2) + ",";
   json += "\"mt5_volume_min\":" + DoubleToString(vol_min, 2) + ",";
   json += "\"mt5_volume_max\":" + DoubleToString(vol_max, 2) + ",";
   json += "\"mt5_volume_step\":" + DoubleToString(vol_step, 2);
   json += "}";

   SendToServer(json);
}

void SendPendingCloseSignal(const long ticket, const string symbol)
{
   string json = "{";
   json += "\"event_type\":\"PENDING_CLOSE\",";
   json += "\"ticket\":" + (string)ticket;

   if(symbol != "")
      json += ",\"symbol\":\"" + JsonEscape(symbol) + "\"";

   json += "}";

   SendToServer(json);
}

//======================================================
// Public hook for OnTradeTransaction (CLOSE via trans)
//======================================================
void Pendings_OnTradeTransaction(const MqlTradeTransaction &trans)
{
   if(trans.order == 0) return;

   // Optional: keep snapshots for other purposes
   if(trans.type == TRADE_TRANSACTION_ORDER_ADD || trans.type == TRADE_TRANSACTION_ORDER_UPDATE)
   {
      UpsertPendSnap_FromLiveOrder((ulong)trans.order);
      return;
   }

   if(trans.type != TRADE_TRANSACTION_ORDER_DELETE)
      return;

   if(!IsPendingOrderType((int)trans.order_type))
      return;

   // Only treat as pending "close" when canceled/expired (ORDER_DELETE also happens on fill)
   ENUM_ORDER_STATE os = (ENUM_ORDER_STATE)trans.order_state;
   if(os != ORDER_STATE_CANCELED && os != ORDER_STATE_EXPIRED)
      return;

   long t = (long)trans.order;
   string sym = trans.symbol;

   PrintFormat("DEBUG PENDING_CLOSE (trans): ticket=%I64d symbol=%s order_type=%s order_state=%s price=%.5f volume=%.2f",
               t,
               sym,
               EnumToString((ENUM_ORDER_TYPE)trans.order_type),
               EnumToString(os),
               trans.price,
               trans.volume);

   SendPendingCloseSignal(t, sym);
   RememberClosedTicket(t);
   RemovePendSnap(t);
}

//======================================================
// Detect new + removed pending orders (polling fallback)
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
      if(ticket_u == 0) continue;
      if(!OrderSelect(ticket_u)) continue;

      int ord_type = (int)OrderGetInteger(ORDER_TYPE);
      if(!IsPendingOrderType(ord_type)) continue;

      long magic = (long)OrderGetInteger(ORDER_MAGIC);
      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      UpsertPendSnap_FromLiveOrder(ticket_u);

      ArrayResize(currTickets, currCount + 1);
      currTickets[currCount] = (long)ticket_u;
      currCount++;

      if(!PendingAlreadySent((long)ticket_u))
      {
         SendPendingOpenSignal(ticket_u);
         MarkPendingSent((long)ticket_u);
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

   // detect removed
   for(int i = 0; i < prevCount; i++)
   {
      long t = prevTickets[i];
      bool existsNow = false;

      for(int j = 0; j < currCount; j++)
      {
         if(currTickets[j] == t) { existsNow = true; break; }
      }

      if(!existsNow)
      {
         if(WasRecentlyClosed(t))
         {
            PrintFormat("DEBUG PENDING_CLOSE (polling) SKIP recent TT: ticket=%I64d", t);
         }
         else
         {
            PrintFormat("DEBUG PENDING_CLOSE (polling): ticket=%I64d", t);
            SendPendingCloseSignal(t, "");
            RememberClosedTicket(t);
         }

         RemovePendSnap(t);
      }
   }

   ArrayFree(prevTickets);
   ArrayCopy(prevTickets, currTickets, 0, 0, WHOLE_ARRAY);
   prevCount = ArraySize(prevTickets);
   UpdatePendingList();
}

#endif // __COPYTRADER_PENDINGS_MQH__
