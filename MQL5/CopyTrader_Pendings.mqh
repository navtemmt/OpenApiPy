//+------------------------------------------------------------------+
//| CopyTrader_Pendings.mqh                                          |
//| Pending orders tracking + PENDING_OPEN + PENDING_CLOSE (MT5 safe)|
//+------------------------------------------------------------------+
#ifndef __COPYTRADER_PENDINGS_MQH__
#define __COPYTRADER_PENDINGS_MQH__

// Requires CopyTrader_State.mqh included BEFORE this file:
// PendingInfo g_lastPendings[]; int g_lastPendingCount;
// long g_sentPendingTickets[];  int g_sentPendingCount.

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
      if(ticket == 0)
         continue;

      if(!OrderSelect(ticket))
         continue;

      int ord_type = (int)OrderGetInteger(ORDER_TYPE);
      if(!IsPendingOrderType(ord_type))
         continue;

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

void SendPendingOpenSignal(const ulong ticket)
{
   if(!OrderSelect(ticket))
   {
      Print("SendPendingOpenSignal: OrderSelect failed for ", ticket, " err=", GetLastError());
      return;
   }

   string   symbol          = OrderGetString(ORDER_SYMBOL);
   int      ord_type        = (int)OrderGetInteger(ORDER_TYPE);
   double   volume          = OrderGetDouble(ORDER_VOLUME_CURRENT);
   double   price_open      = OrderGetDouble(ORDER_PRICE_OPEN);
   double   price_stoplimit = OrderGetDouble(ORDER_PRICE_STOPLIMIT);
   double   sl              = OrderGetDouble(ORDER_SL);
   double   tp              = OrderGetDouble(ORDER_TP);
   long     magic           = (long)OrderGetInteger(ORDER_MAGIC);
   datetime exp             = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);

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
   json += "\"magic\":" + (string)magic + ",";
   json += "\"expiration_ms\":" + (string)exp_ms + ",";
   json += "\"mt5_contract_size\":" + DoubleToString(contract_size, 2) + ",";
   json += "\"mt5_volume_min\":" + DoubleToString(vol_min, 2) + ",";
   json += "\"mt5_volume_max\":" + DoubleToString(vol_max, 2) + ",";
   json += "\"mt5_volume_step\":" + DoubleToString(vol_step, 2);
   json += "}";

   PrintFormat("DEBUG SEND PENDING_OPEN ticket=%I64u symbol=%s magic=%I64d", ticket, symbol, magic);
   SendToServer(json);
}

void SendPendingCloseSignal(const long ticket,
                            const string symbol,
                            const long magic)
{
   PrintFormat("DEBUG SEND PENDING_CLOSE ticket=%I64d symbol=%s magic=%I64d",
               ticket, symbol, magic);

   string json = "{";
   json += "\"event_type\":\"PENDING_CLOSE\",";
   json += "\"ticket\":" + (string)ticket;

   if(symbol != "")
      json += ",\"symbol\":\"" + JsonEscape(symbol) + "\"";

   json += ",\"magic\":" + (string)magic;
   json += "}";

   SendToServer(json);
}

//======================================================
// Detect new + removed pending orders (robust snapshot)
//======================================================
void CheckPendingChanges()
{
   static long prevTickets[];
   static int  prevCount = -1;

   int totalOrders = OrdersTotal();
   PrintFormat("DEBUG CheckPendingChanges: OrdersTotal=%d lastPending=%d filter='%s'",
               totalOrders, g_lastPendingCount, MagicNumberFilter);

   long currTickets[];
   int  currCount = 0;

   // Build current tickets (filtered) + OPEN for new
   for(int i = 0; i < totalOrders; i++)
   {
      ulong ticket_u = OrderGetTicket(i);
      if(ticket_u == 0)
         continue;

      if(!OrderSelect(ticket_u))
         continue;

      int ord_type = (int)OrderGetInteger(ORDER_TYPE);
      if(!IsPendingOrderType(ord_type))
         continue;

      long magic = (long)OrderGetInteger(ORDER_MAGIC);
      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      string sym = OrderGetString(ORDER_SYMBOL);
      PrintFormat("DEBUG pending seen: ticket=%I64u type=%d magic=%I64d symbol=%s",
                  ticket_u, ord_type, magic, sym);

      ArrayResize(currTickets, currCount + 1);
      currTickets[currCount] = (long)ticket_u;
      currCount++;

      if(!PendingAlreadySent((long)ticket_u))
      {
         PrintFormat("DEBUG pending new -> OPEN ticket=%I64u", ticket_u);
         SendPendingOpenSignal(ticket_u);
         MarkPendingSent((long)ticket_u);
      }
   }

   // Initialize snapshot (no CLOSE on first run)
   if(prevCount < 0)
   {
      ArrayFree(prevTickets);
      ArrayCopy(prevTickets, currTickets, 0, 0, WHOLE_ARRAY);
      prevCount = ArraySize(prevTickets);

      PrintFormat("DEBUG pending init snapshot prevCount=%d", prevCount);
      UpdatePendingList();
      return;
   }

   // Removals: in prevTickets but not in currTickets
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
         string sym = "";
         long   mg  = 0;

         for(int k = 0; k < g_lastPendingCount; k++)
         {
            if(g_lastPendings[k].ticket == t)
            {
               sym = g_lastPendings[k].symbol;
               mg  = g_lastPendings[k].magicNumber;
               break;
            }
         }

         PrintFormat("DEBUG pending removed -> CLOSE ticket=%I64d symbol=%s magic=%I64d",
                     t, sym, mg);

         SendPendingCloseSignal(t, sym, mg);
      }
   }

   // Update snapshot
   ArrayFree(prevTickets);
   ArrayCopy(prevTickets, currTickets, 0, 0, WHOLE_ARRAY);
   prevCount = ArraySize(prevTickets);

   PrintFormat("DEBUG pending snapshot updated prevCount=%d currCount=%d", prevCount, currCount);

   UpdatePendingList();
}

#endif // __COPYTRADER_PENDINGS_MQH__
