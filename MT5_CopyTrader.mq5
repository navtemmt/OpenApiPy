//+------------------------------------------------------------------+
//|                                           MT5_CopyTrader.mq5     |
//|                                  MT5 to cTrader Copy Trading EA  |
//+------------------------------------------------------------------+
#property copyright "Copyright 2025"
#property version   "1.09"
#property strict

// Input parameters
input string BridgeServerURL   = "http://127.0.0.1:3140";  // Python bridge server URL
input int    RequestTimeout    = 5000;                     // HTTP request timeout in ms
input string MagicNumberFilter = "";                       // Filter by magic number (empty = all trades)
input bool   CopyPendingOrders = true;                     // Copy pending orders (LIMIT/STOP/STOP_LIMIT)

//+------------------------------------------------------------------+
//| Structs                                                          |
//+------------------------------------------------------------------+
struct TradeInfo {
   long   ticket;
   string symbol;
   int    type;
   double volume;
   double openPrice;
   double stopLoss;
   double takeProfit;
   long   magicNumber;
};

struct PendingInfo {
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

TradeInfo   g_lastTrades[];
int         g_lastTradeCount = 0;

PendingInfo g_lastPendings[];
int         g_lastPendingCount = 0;

// Track which pending tickets we've already sent (avoid duplicates)
long g_sentPendingTickets[];
int  g_sentPendingCount = 0;

//+------------------------------------------------------------------+
//| Helper: get symbol trade metadata                                |
//+------------------------------------------------------------------+
bool GetSymbolTradeMeta(const string symbol,
                        double &contract_size,
                        double &vol_min,
                        double &vol_max,
                        double &vol_step)
{
   contract_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   vol_min       = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   vol_max       = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   vol_step      = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

   if(contract_size <= 0.0)
   {
      Print("GetSymbolTradeMeta: invalid contract_size for ", symbol, " = ", contract_size);
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
//| Helper: escape string for JSON                                   |
//+------------------------------------------------------------------+
string JsonEscape(const string s)
{
   string out = s;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   return out;
}

//+------------------------------------------------------------------+
//| Helper: pending already sent?                                    |
//+------------------------------------------------------------------+
bool PendingAlreadySent(const long ticket)
{
   for(int i=0;i<g_sentPendingCount;i++)
      if(g_sentPendingTickets[i] == ticket)
         return true;
   return false;
}

void MarkPendingSent(const long ticket)
{
   if(PendingAlreadySent(ticket))
      return;
   ArrayResize(g_sentPendingTickets, g_sentPendingCount+1);
   g_sentPendingTickets[g_sentPendingCount] = ticket;
   g_sentPendingCount++;
}

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   Print("MT5 CopyTrader EA initialized. Bridge server: ", BridgeServerURL);
   UpdateTradeList();
   UpdatePendingList();
   Print("Initial positions tracked: ", g_lastTradeCount, ", pending tracked: ", g_lastPendingCount);

   // Send existing pending once at startup (optional but useful)
   if(CopyPendingOrders)
      CheckPendingChanges();

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("MT5 CopyTrader EA stopped. Reason: ", reason);
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
   CheckTradeChanges();

   if(CopyPendingOrders)
      CheckPendingChanges();
}

//+------------------------------------------------------------------+
//| Update current trade list (dense indexing)                       |
//+------------------------------------------------------------------+
void UpdateTradeList()
{
   int totalPositions = PositionsTotal();
   ArrayResize(g_lastTrades, totalPositions);

   int idx = 0;
   for(int i = 0; i < totalPositions; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      long magic = PositionGetInteger(POSITION_MAGIC);
      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      string sym = PositionGetString(POSITION_SYMBOL);

      g_lastTrades[idx].ticket      = (long)ticket;
      g_lastTrades[idx].symbol      = sym;
      g_lastTrades[idx].type        = (int)PositionGetInteger(POSITION_TYPE);
      g_lastTrades[idx].volume      = PositionGetDouble(POSITION_VOLUME);
      g_lastTrades[idx].openPrice   = PositionGetDouble(POSITION_PRICE_OPEN);
      g_lastTrades[idx].stopLoss    = PositionGetDouble(POSITION_SL);
      g_lastTrades[idx].takeProfit  = PositionGetDouble(POSITION_TP);
      g_lastTrades[idx].magicNumber = magic;

      idx++;
   }

   g_lastTradeCount = idx;
   ArrayResize(g_lastTrades, g_lastTradeCount);
}

//+------------------------------------------------------------------+
//| Update current pending orders list (dense indexing)              |
//+------------------------------------------------------------------+
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
      if(ord_type != ORDER_TYPE_BUY_LIMIT &&
         ord_type != ORDER_TYPE_SELL_LIMIT &&
         ord_type != ORDER_TYPE_BUY_STOP  &&
         ord_type != ORDER_TYPE_SELL_STOP &&
         ord_type != ORDER_TYPE_BUY_STOP_LIMIT &&
         ord_type != ORDER_TYPE_SELL_STOP_LIMIT)
      {
         continue;
      }

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

//+------------------------------------------------------------------+
//| Check for trade changes and send to bridge                       |
//+------------------------------------------------------------------+
void CheckTradeChanges()
{
   int currentPositions = PositionsTotal();

   for(int i = 0; i < currentPositions; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      long   magic  = PositionGetInteger(POSITION_MAGIC);

      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      double currentVol = PositionGetDouble(POSITION_VOLUME);
      double currentSL  = PositionGetDouble(POSITION_SL);
      double currentTP  = PositionGetDouble(POSITION_TP);

      bool isNew = true;
      for(int j = 0; j < g_lastTradeCount; j++)
      {
         if(g_lastTrades[j].ticket == (long)ticket)
         {
            isNew = false;

            if(currentVol < g_lastTrades[j].volume)
            {
               double closedPart = g_lastTrades[j].volume - currentVol;
               PrintFormat("Partial close detected: ticket=%I64u symbol=%s oldVol=%.2f newVol=%.2f closedPart=%.2f",
                           ticket, symbol, g_lastTrades[j].volume, currentVol, closedPart);

               SendCloseSignal((long)ticket, symbol, closedPart);
               g_lastTrades[j].volume = currentVol;
            }

            if(currentSL != g_lastTrades[j].stopLoss || currentTP != g_lastTrades[j].takeProfit)
            {
               SendModifySignal(ticket, currentSL, currentTP);
               g_lastTrades[j].stopLoss   = currentSL;
               g_lastTrades[j].takeProfit = currentTP;
            }
            break;
         }
      }

      if(isNew)
      {
         SendOpenSignal(ticket);
      }
   }

   for(int i = 0; i < g_lastTradeCount; i++)
   {
      bool exists = false;
      for(int j = 0; j < currentPositions; j++)
      {
         ulong ticket = PositionGetTicket(j);
         if((long)ticket == g_lastTrades[i].ticket)
         {
            exists = true;
            break;
         }
      }

      if(!exists)
      {
         long   ticket  = g_lastTrades[i].ticket;
         string symbol  = g_lastTrades[i].symbol;
         double volume  = g_lastTrades[i].volume;

         PrintFormat("Full close detected: ticket=%I64d symbol=%s lastVol=%.2f",
                     ticket, symbol, volume);

         SendCloseSignal(ticket, symbol, volume);
      }
   }

   UpdateTradeList();
}

//+------------------------------------------------------------------+
//| Check for pending order changes and send to bridge               |
//+------------------------------------------------------------------+
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
      if(ord_type != ORDER_TYPE_BUY_LIMIT &&
         ord_type != ORDER_TYPE_SELL_LIMIT &&
         ord_type != ORDER_TYPE_BUY_STOP  &&
         ord_type != ORDER_TYPE_SELL_STOP &&
         ord_type != ORDER_TYPE_BUY_STOP_LIMIT &&
         ord_type != ORDER_TYPE_SELL_STOP_LIMIT)
      {
         continue;
      }

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

//+------------------------------------------------------------------+
//| Send open trade signal to bridge server                          |
//+------------------------------------------------------------------+
void SendOpenSignal(ulong ticket)
{
   if(!PositionSelectByTicket(ticket))
   {
      Print("SendOpenSignal: PositionSelectByTicket failed for ", ticket);
      return;
   }

   string symbol    = PositionGetString(POSITION_SYMBOL);
   int    type      = (int)PositionGetInteger(POSITION_TYPE);
   double volume    = PositionGetDouble(POSITION_VOLUME);
   double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   double sl        = PositionGetDouble(POSITION_SL);
   double tp        = PositionGetDouble(POSITION_TP);
   long   magic     = PositionGetInteger(POSITION_MAGIC);

   double contract_size, vol_min, vol_max, vol_step;
   GetSymbolTradeMeta(symbol, contract_size, vol_min, vol_max, vol_step);

   string tradeType = (type == POSITION_TYPE_BUY) ? "BUY" : "SELL";

   string jsonData = "{"
      "\"action\":\"OPEN\","
      "\"ticket\":" + (string)ticket + ","
      "\"symbol\":\"" + JsonEscape(symbol) + "\","
      "\"type\":\"" + tradeType + "\","
      "\"volume\":" + DoubleToString(volume, 2) + ","
      "\"price\":" + DoubleToString(openPrice, 5) + ","
      "\"sl\":" + DoubleToString(sl, 5) + ","
      "\"tp\":" + DoubleToString(tp, 5) + ","
      "\"magic\":" + (string)magic + ","
      "\"mt5_contract_size\":" + DoubleToString(contract_size, 2) + ","
      "\"mt5_volume_min\":" + DoubleToString(vol_min, 2) + ","
      "\"mt5_volume_max\":" + DoubleToString(vol_max, 2) + ","
      "\"mt5_volume_step\":" + DoubleToString(vol_step, 2) +
      "}";

   SendToServer(jsonData);
   Print("Sent OPEN signal for ticket #", ticket, ": ", symbol, " ", tradeType, " ", volume);
}

//+------------------------------------------------------------------+
//| Send pending order OPEN signal to bridge server                  |
//+------------------------------------------------------------------+
void SendPendingOpenSignal(ulong ticket)
{
   // OrderSelect(ticket) already done in caller, but keep safe:
   if(!OrderSelect(ticket))
   {
      Print("SendPendingOpenSignal: OrderSelect failed for ", ticket, " err=", GetLastError());
      return;
   }

   string symbol = OrderGetString(ORDER_SYMBOL);
   int ord_type  = (int)OrderGetInteger(ORDER_TYPE);
   double volume = OrderGetDouble(ORDER_VOLUME_CURRENT);

   double price_open      = OrderGetDouble(ORDER_PRICE_OPEN);
   double price_stoplimit = OrderGetDouble(ORDER_PRICE_STOPLIMIT);

   double sl = OrderGetDouble(ORDER_SL);
   double tp = OrderGetDouble(ORDER_TP);
   long magic = (long)OrderGetInteger(ORDER_MAGIC);
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

//+------------------------------------------------------------------+
//| Send close trade signal to bridge server (full or partial)       |
//+------------------------------------------------------------------+
void SendCloseSignal(long ticket, string symbol, double closedVolume)
{
   double contract_size = 0.0, vol_min = 0.0, vol_max = 0.0, vol_step = 0.0;
   if(symbol != "")
      GetSymbolTradeMeta(symbol, contract_size, vol_min, vol_max, vol_step);

   string jsonData = "{"
      "\"action\":\"CLOSE\","
      "\"ticket\":" + (string)ticket + ",";

   if(symbol != "")
      jsonData += "\"symbol\":\"" + JsonEscape(symbol) + "\",";

   jsonData += "\"volume\":" + DoubleToString(closedVolume, 8);

   if(symbol != "" && contract_size > 0.0)
   {
      jsonData += ",\"mt5_contract_size\":" + DoubleToString(contract_size, 2);
      jsonData += ",\"mt5_volume_min\":" + DoubleToString(vol_min, 2);
      jsonData += ",\"mt5_volume_max\":" + DoubleToString(vol_max, 2);
      jsonData += ",\"mt5_volume_step\":" + DoubleToString(vol_step, 2);
   }

   jsonData += "}";

   SendToServer(jsonData);
   Print("Sent CLOSE signal for ticket #", ticket,
         " symbol=", symbol, " closedVolume=", closedVolume);
}

//+------------------------------------------------------------------+
//| Send modify trade signal to bridge server                        |
//+------------------------------------------------------------------+
void SendModifySignal(ulong ticket, double sl, double tp)
{
   string symbol = "";
   for(int i = 0; i < g_lastTradeCount; i++)
   {
      if(g_lastTrades[i].ticket == (long)ticket)
      {
         symbol = g_lastTrades[i].symbol;
         break;
      }
   }

   string jsonData = "{"
      "\"action\":\"MODIFY\","
      "\"ticket\":" + (string)ticket + ",";

   if(symbol != "")
      jsonData += "\"symbol\":\"" + JsonEscape(symbol) + "\",";

   jsonData +=
      "\"sl\":" + DoubleToString(sl, 5) + ","
      "\"tp\":" + DoubleToString(tp, 5) +
      "}";

   SendToServer(jsonData);
   Print("Sent MODIFY signal for ticket #", ticket, ": ", symbol, " SL=", sl, " TP=", tp);
}

//+------------------------------------------------------------------+
//| Send HTTP POST request to bridge server                          |
//+------------------------------------------------------------------+
void SendToServer(string jsonData)
{
   char   post[];
   char   result[];
   string headers;

   Print("DEBUG JSON -> ", jsonData);

   StringToCharArray(jsonData, post, 0, StringLen(jsonData));

   string url = BridgeServerURL + "/trade_signal";
   headers    = "Content-Type: application/json\r\n";

   ResetLastError();
   int res = WebRequest(
      "POST",
      url,
      headers,
      RequestTimeout,
      post,
      result,
      headers
   );

   if(res == -1)
   {
      int error = GetLastError();
      Print("WebRequest error: ", error,
            ". Make sure URL is added to allowed URLs in Tools > Options > Expert Advisors");
      return;
   }

   if(res == 200)
      Print("Signal sent successfully to bridge server");
   else
      Print("Bridge server returned status code: ", res);
}
//+------------------------------------------------------------------+
