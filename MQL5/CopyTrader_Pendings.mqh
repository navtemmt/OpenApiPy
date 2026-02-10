//+------------------------------------------------------------------+
//| CopyTrader_Pendings.mqh                                          |
//| Pending orders tracking + PENDING_OPEN + PENDING_CLOSE (MT5 safe)|
//+------------------------------------------------------------------+
#ifndef __COPYTRADER_PENDINGS_MQH__
#define __COPYTRADER_PENDINGS_MQH__

// Requires CopyTrader_State.mqh included BEFORE this file:
// PendingInfo g_lastPendings[]; int g_lastPendingCount;
// long g_sentPendingTickets[];  int g_sentPendingCount. [page:679]

//======================================================
// Helpers
//======================================================
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
// Snapshot current pendings
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

//======================================================
// Send pending OPEN signal
//======================================================
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
   if(exp > 0)
      exp_ms = (long)exp * 1000;

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

   SendToServer(json);
}

//======================================================
// Send pending CLOSE signal (removed from MT5 order pool)
//======================================================
void SendPendingCloseSignal(const long ticket,
                            const string symbol,
                            const long magic)
{
   string json = "{";
   json += "\"event_type\":\"PENDING_CLOSE\",";
   json += "\"ticket\":" + (string)ticket;

   if(symbol != "")
      json += ",\"symbol\":\"" + JsonEscape(symbol) + "\"";

   json += ",\"magic\":" + (string)magic;
   json += "}";

   SendToServer(json);
   PrintFormat("Sent PENDING_CLOSE signal for ticket #%I64d symbol=%s magic=%I64d",
               ticket, symbol, magic);
}

//======================================================
// Detect new + removed pending orders
//======================================================
void CheckPendingChanges()
{
   int totalOrders = OrdersTotal();

   // Build list of current pending tickets (filtered)
   long currentTickets[];
   int  currentCount = 0;

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

      // Track current pending ticket
      ArrayResize(currentTickets, currentCount + 1);
      currentTickets[currentCount] = (long)ticket_u;
      currentCount++;

      // New pending -> send once
      if(!PendingAlreadySent((long)ticket_u))
      {
         SendPendingOpenSignal(ticket_u);
         MarkPendingSent((long)ticket_u);
      }
   }

   // Removed pendings: existed in last snapshot, not present now
   for(int i = 0; i < g_lastPendingCount; i++)
   {
      long lastTicket = g_lastPendings[i].ticket;

      bool existsNow = false;
      for(int j = 0; j < currentCount; j++)
      {
         if(currentTickets[j] == lastTicket)
         {
            existsNow = true;
            break;
         }
      }

      if(!existsNow)
      {
         SendPendingCloseSignal(lastTicket,
                                g_lastPendings[i].symbol,
                                g_lastPendings[i].magicNumber);
      }
   }

   UpdatePendingList();
}

#endif // __COPYTRADER_PENDINGS_MQH__
