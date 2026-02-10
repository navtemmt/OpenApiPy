//+------------------------------------------------------------------+
//| CopyTrader_Pendings.mqh                                          |
//| Pending orders tracking + PENDING_OPEN signals (v1.09 style)     |
//+------------------------------------------------------------------+
#pragma once

// Expects these globals/inputs from your project:
// input string MagicNumberFilter;
// input bool   CopyPendingOrders;
// PendingInfo g_lastPendings[]; int g_lastPendingCount;
// long g_sentPendingTickets[]; int g_sentPendingCount;
//
// Expects these functions from your other modules:
// string JsonEscape(const string s);
// bool GetSymbolTradeMeta(const string symbol, double &contract_size, double &vol_min, double &vol_max, double &vol_step);
// void SendToServer(string jsonData);

// -----------------------------
// Sent-cache helpers
// -----------------------------
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

// -----------------------------
// Internal helper: pending type?
// -----------------------------
bool IsPendingOrderType(const int ord_type)
{
   return (ord_type == ORDER_TYPE_BUY_LIMIT ||
           ord_type == ORDER_TYPE_SELL_LIMIT ||
           ord_type == ORDER_TYPE_BUY_STOP ||
           ord_type == ORDER_TYPE_SELL_STOP ||
           ord_type == ORDER_TYPE_BUY_STOP_LIMIT ||
           ord_type == ORDER_TYPE_SELL_STOP_LIMIT);
}

// -----------------------------
// Update snapshot of pendings
// -----------------------------
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

// -----------------------------
// Signal: PENDING_OPEN
// -----------------------------
void SendPendingOpenSignal(ulong ticket)
{
   // Caller usually already selected, but keep safe
   if(!OrderSelect(ticket))
   {
      Print("SendPendingOpenSignal: OrderSelect failed for ", ticket, " err=", GetLastError());
      return;
   }

   string symbol     = OrderGetString(ORDER_SYMBOL);
   int    ord_type   = (int)OrderGetInteger(ORDER_TYPE);
   double volume     = OrderGetDouble(ORDER_VOLUME_CURRENT);

   double price_open      = OrderGetDouble(ORDER_PRICE_OPEN);
   double price_stoplimit = OrderGetDouble(ORDER_PRICE_STOPLIMIT);

   double sl    = OrderGetDouble(ORDER_SL);
   double tp    = OrderGetDouble(ORDER_TP);
   long   magic = (long)OrderGetInteger(ORDER_MAGIC);
   datetime exp = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);

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

   string jsonData = "{"
      "\"event_type\":\"PENDING_OPEN\","
      "\"ticket\":" + (string)ticket + ","
      "\"symbol\":\"" + JsonEscape(symbol) + "\","
      "\"side\":\"" + side + "\","
      "\"volume\":" + DoubleToString(volume, 2) + ","
      "\"pending_type\":\"" + pending_type + "\",";

   if(pending_type == "limit")
      jsonData += "\"limit_price\":" + DoubleToString(price_open, 5) + ",";
   else if(pending_type == "stop")
      jsonData += "\"stop_price\":" + DoubleToString(price_open, 5) + ",";
   else
   {
      jsonData += "\"stop_price\":" + DoubleToString(price_open, 5) + ",";
      jsonData += "\"limit_price\":" + DoubleToString(price_stoplimit, 5) + ",";
   }

   jsonData +=
      "\"sl\":" + DoubleToString(sl, 5) + ","
      "\"tp\":" + DoubleToString(tp, 5) + ","
      "\"magic\":" + (string)magic + ","
      "\"expiration_ms\":" + (string)exp_ms + ","
      "\"mt5_contract_size\":" + DoubleToString(contract_size, 2) + ","
      "\"mt5_volume_min\":" + DoubleToString(vol_min, 2) + ","
      "\"mt5_volume_max\":" + DoubleToString(vol_max, 2) + ","
      "\"mt5_volume_step\":" + DoubleToString(vol_step, 2) +
   "}";

   SendToServer(jsonData);

   PrintFormat("Sent PENDING_OPEN signal for order #%I64u: %s %s vol=%.2f",
               ticket, symbol, pending_type, volume);
}

// -----------------------------
// Detect new pendings and send once
// -----------------------------
void CheckPendingChanges()
{
   int currentOrders = OrdersTotal();
   if(currentOrders <= 0)
   {
      UpdatePendingList();
      return;
   }

   for(int i = 0; i < currentOrders; i++)
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

      if(PendingAlreadySent((long)ticket))
         continue;

      SendPendingOpenSignal(ticket);
      MarkPendingSent((long)ticket);
   }

   UpdatePendingList();
}
