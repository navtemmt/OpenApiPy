#ifndef COPYTRADER_PENDINGS_MQH
#define COPYTRADER_PENDINGS_MQH

// Requires CopyTrader_State.mqh included BEFORE this file:
// PendingInfo g_lastPendings[]; int g_lastPendingCount;
// long g_sentPendingTickets[]; int g_sentPendingCount.
//
// Also requires these helpers somewhere in your project:
// - string JsonEscape(const string s);
// - void SendToServer(string json);
// - bool GetSymbolTradeMeta(const string symbol, double &contract_size, double &vol_min, double &vol_max, double &vol_step);
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
   return (ord_type == OP_BUYLIMIT ||
           ord_type == OP_SELLLIMIT ||
           ord_type == OP_BUYSTOP ||
           ord_type == OP_SELLSTOP);
}

//======================================================
// CLOSE de-dupe cache
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
// Snapshot store
//======================================================
struct PendingSnap
{
   long     ticket;
   string   symbol;
   int      type;
   double   volume;
   double   priceOpen;
   double   priceStopLimit;
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

bool UpsertPendSnapFromLiveOrder(const int ticket)
{
   if(ticket <= 0)
      return false;

   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return false;

   int ord_type = OrderType();
   if(!IsPendingOrderType(ord_type))
      return false;

   long magic = OrderMagicNumber();
   if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
      return false;

   int idx = FindPendSnapIndex(ticket);
   if(idx < 0)
   {
      ArrayResize(g_pendSnap, g_pendSnapCount + 1);
      idx = g_pendSnapCount++;
   }

   g_pendSnap[idx].ticket         = ticket;
   g_pendSnap[idx].symbol         = OrderSymbol();
   g_pendSnap[idx].type           = ord_type;
   g_pendSnap[idx].volume         = OrderLots();
   g_pendSnap[idx].priceOpen      = OrderOpenPrice();
   g_pendSnap[idx].priceStopLimit = 0.0;
   g_pendSnap[idx].stopLoss       = OrderStopLoss();
   g_pendSnap[idx].takeProfit     = OrderTakeProfit();
   g_pendSnap[idx].magicNumber    = magic;
   g_pendSnap[idx].expiration     = OrderExpiration();

   return true;
}

//======================================================
// Snapshot current pendings
//======================================================
void UpdatePendingList()
{
   int totalOrders = OrdersTotal();
   ArrayResize(g_lastPendings, totalOrders);

   int idx = 0;

   for(int i = 0; i < totalOrders; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;

      int ord_type = OrderType();
      if(!IsPendingOrderType(ord_type))
         continue;

      long magic = OrderMagicNumber();
      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      g_lastPendings[idx].ticket         = OrderTicket();
      g_lastPendings[idx].symbol         = OrderSymbol();
      g_lastPendings[idx].type           = ord_type;
      g_lastPendings[idx].volume         = OrderLots();
      g_lastPendings[idx].priceOpen      = OrderOpenPrice();
      g_lastPendings[idx].priceStopLimit = 0.0;
      g_lastPendings[idx].stopLoss       = OrderStopLoss();
      g_lastPendings[idx].takeProfit     = OrderTakeProfit();
      g_lastPendings[idx].magicNumber    = magic;
      g_lastPendings[idx].expiration     = OrderExpiration();

      idx++;
   }

   g_lastPendingCount = idx;
   ArrayResize(g_lastPendings, g_lastPendingCount);
}

//======================================================
// JSON builders
//======================================================
void SendPendingOpenSignal(const int ticket)
{
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
   {
      Print("SendPendingOpenSignal: OrderSelect failed for ", ticket, " err=", GetLastError());
      return;
   }

   string symbol   = OrderSymbol();
   int ord_type    = OrderType();
   double volume   = OrderLots();
   double price    = OrderOpenPrice();
   double sl       = OrderStopLoss();
   double tp       = OrderTakeProfit();
   datetime exp    = OrderExpiration();

   double contract_size, vol_min, vol_max, vol_step;
   GetSymbolTradeMeta(symbol, contract_size, vol_min, vol_max, vol_step);

   string side = "BUY";
   string pending_type = "limit";

   if(ord_type == OP_BUYLIMIT)  { side = "BUY";  pending_type = "limit"; }
   if(ord_type == OP_SELLLIMIT) { side = "SELL"; pending_type = "limit"; }
   if(ord_type == OP_BUYSTOP)   { side = "BUY";  pending_type = "stop";  }
   if(ord_type == OP_SELLSTOP)  { side = "SELL"; pending_type = "stop";  }

   long exp_ms = 0;
   if(exp > 0)
      exp_ms = (long)exp * 1000;

   int priceDigits = (int)MarketInfo(symbol, MODE_DIGITS);

   string json = "{";
   json += "\"event_type\":\"PENDING_OPEN\",";
   json += "\"ticket\":" + IntegerToString(ticket) + ",";
   json += "\"symbol\":\"" + JsonEscape(symbol) + "\",";
   json += "\"side\":\"" + side + "\",";
   json += "\"volume\":" + DoubleToString(volume, 2) + ",";
   json += "\"pending_type\":\"" + pending_type + "\",";

   if(pending_type == "limit")
      json += "\"limit_price\":" + DoubleToString(price, priceDigits) + ",";
   else
      json += "\"stop_price\":" + DoubleToString(price, priceDigits) + ",";

   json += "\"sl\":" + DoubleToString(sl, priceDigits) + ",";
   json += "\"tp\":" + DoubleToString(tp, priceDigits) + ",";
   json += "\"expiration_ms\":\"" + IntegerToString((int)exp_ms) + "\",";
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
   json += "\"ticket\":" + IntegerToString((int)ticket);

   if(symbol != "")
      json += ",\"symbol\":\"" + JsonEscape(symbol) + "\"";

   json += "}";

   SendToServer(json);
}

//======================================================
// Detect new + removed pending orders
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
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;

      int ord_type = OrderType();
      if(!IsPendingOrderType(ord_type))
         continue;

      long magic = OrderMagicNumber();
      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      int ticket = OrderTicket();

      UpsertPendSnapFromLiveOrder(ticket);

      ArrayResize(currTickets, currCount + 1);
      currTickets[currCount] = ticket;
      currCount++;

      if(!PendingAlreadySent(ticket))
      {
         SendPendingOpenSignal(ticket);
         MarkPendingSent(ticket);
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

      if(!existsNow)
      {
         if(WasRecentlyClosed(t))
         {
            PrintFormat("DEBUG PENDING_CLOSE skip recent: ticket=%d", (int)t);
         }
         else
         {
            PrintFormat("DEBUG PENDING_CLOSE: ticket=%d", (int)t);
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

#endif // COPYTRADER_PENDINGS_MQH
