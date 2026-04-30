#ifndef COPYTRADER_SIGNALS_MQH
#define COPYTRADER_SIGNALS_MQH

bool GetSymbolTradeMeta(const string symbol,
                        double &contract_size,
                        double &vol_min,
                        double &vol_max,
                        double &vol_step)
{
   contract_size = MarketInfo(symbol, MODE_LOTSIZE);
   vol_min       = MarketInfo(symbol, MODE_MINLOT);
   vol_max       = MarketInfo(symbol, MODE_MAXLOT);
   vol_step      = MarketInfo(symbol, MODE_LOTSTEP);

   if(contract_size <= 0.0)
   {
      Print("GetSymbolTradeMeta: invalid contract_size for ", symbol, " = ", contract_size);
      return false;
   }

   return true;
}

double GetMt5TickSize(const string symbol)
{
   double v = MarketInfo(symbol, MODE_TICKSIZE);
   if(v <= 0.0)
      v = MarketInfo(symbol, MODE_POINT);
   return v;
}

double GetMt5TickValue(const string symbol)
{
   return MarketInfo(symbol, MODE_TICKVALUE);
}

double GetQuoteToDepositRate(const string symbol)
{
   // MT4 usually reports MODE_TICKVALUE in deposit currency already.
   // Keep 1.0 as default unless you add real cross-currency conversion logic.
   return 1.0;
}

void SendOpenSignal(int ticket, bool startupSync = false)
{
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
   {
      Print("SendOpenSignal: OrderSelect failed for ", ticket);
      return;
   }

   string symbol    = OrderSymbol();
   int    type      = OrderType();
   double volume    = OrderLots();
   double openPrice = OrderOpenPrice();
   double sl        = OrderStopLoss();
   double tp        = OrderTakeProfit();
   int    magic     = OrderMagicNumber();

   double contract_size = 0.0;
   double vol_min       = 0.0;
   double vol_max       = 0.0;
   double vol_step      = 0.0;
   GetSymbolTradeMeta(symbol, contract_size, vol_min, vol_max, vol_step);

   string tradeType = (type == OP_BUY) ? "BUY" : "SELL";

   int priceDigits = (int)MarketInfo(symbol, MODE_DIGITS);
   if(priceDigits <= 0)
      priceDigits = Digits;

   double currentBid         = MarketInfo(symbol, MODE_BID);
   double currentAsk         = MarketInfo(symbol, MODE_ASK);
   double pointSize          = MarketInfo(symbol, MODE_POINT);
   double tickSize           = GetMt5TickSize(symbol);
   double tickValue          = GetMt5TickValue(symbol);
   double quoteToDepositRate = GetQuoteToDepositRate(symbol);

   string startupSyncStr = startupSync ? "true" : "false";
   string syncOrigin     = startupSync ? "startup" : "live";

   string jsonData = "{"
      "\"action\":\"OPEN\","
      "\"event_type\":\"OPEN\","
      "\"ticket\":" + IntegerToString(ticket) + ","
      "\"symbol\":\"" + JsonEscape(symbol) + "\","
      "\"type\":\"" + tradeType + "\","
      "\"side\":\"" + tradeType + "\","
      "\"volume\":" + DoubleToString(volume, 2) + ","
      "\"price\":" + DoubleToString(openPrice, priceDigits) + ","
      "\"entry_price\":" + DoubleToString(openPrice, priceDigits) + ","
      "\"open_price\":" + DoubleToString(openPrice, priceDigits) + ","
      "\"current_bid\":" + DoubleToString(currentBid, priceDigits) + ","
      "\"current_ask\":" + DoubleToString(currentAsk, priceDigits) + ","
      "\"point\":" + DoubleToString(pointSize, priceDigits) + ","
      "\"digits\":" + IntegerToString(priceDigits) + ","
      "\"mt5_tick_size\":" + DoubleToString(tickSize, priceDigits) + ","
      "\"mt5_tick_value\":" + DoubleToString(tickValue, 8) + ","
      "\"quote_to_deposit_rate\":" + DoubleToString(quoteToDepositRate, 8) + ","
      "\"sl\":" + DoubleToString(sl, priceDigits) + ","
      "\"tp\":" + DoubleToString(tp, priceDigits) + ","
      "\"magic\":" + IntegerToString(magic) + ","
      "\"startup_sync\":" + startupSyncStr + ","
      "\"sync_origin\":\"" + syncOrigin + "\","
      "\"mt5_contract_size\":" + DoubleToString(contract_size, 2) + ","
      "\"mt5_volume_min\":" + DoubleToString(vol_min, 2) + ","
      "\"mt5_volume_max\":" + DoubleToString(vol_max, 2) + ","
      "\"mt5_volume_step\":" + DoubleToString(vol_step, 2) +
   "}";

   SendToServer(jsonData);

   Print("Sent OPEN signal for ticket #", ticket,
         ": ", symbol, " ", tradeType, " ", DoubleToString(volume, 2),
         " openPrice=", DoubleToString(openPrice, priceDigits),
         " bid=", DoubleToString(currentBid, priceDigits),
         " ask=", DoubleToString(currentAsk, priceDigits),
         " tickSize=", DoubleToString(tickSize, priceDigits),
         " tickValue=", DoubleToString(tickValue, 8),
         " startupSync=", startupSyncStr);
}

void SendCloseSignal(long ticket, string symbol, double closedVolume)
{
   double contract_size = 0.0, vol_min = 0.0, vol_max = 0.0, vol_step = 0.0;

   if(symbol != "")
      GetSymbolTradeMeta(symbol, contract_size, vol_min, vol_max, vol_step);

   string jsonData = "{"
      "\"action\":\"CLOSE\","
      "\"ticket\":" + IntegerToString((int)ticket) + ",";

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

void SendModifySignal(int ticket, double sl, double tp)
{
   string symbol = "";

   for(int i = 0; i < g_lastTradeCount; i++)
   {
      if(g_lastTrades[i].ticket == ticket)
      {
         symbol = g_lastTrades[i].symbol;
         break;
      }
   }

   int priceDigits = Digits;
   if(symbol != "")
      priceDigits = (int)MarketInfo(symbol, MODE_DIGITS);

   string jsonData = "{"
      "\"action\":\"MODIFY\","
      "\"ticket\":" + IntegerToString(ticket) + ",";

   if(symbol != "")
      jsonData += "\"symbol\":\"" + JsonEscape(symbol) + "\",";

   jsonData +=
      "\"sl\":" + DoubleToString(sl, priceDigits) + ","
      "\"tp\":" + DoubleToString(tp, priceDigits) +
   "}";

   SendToServer(jsonData);
   Print("Sent MODIFY signal for ticket #", ticket, ": ", symbol, " SL=", sl, " TP=", tp);
}

#endif // COPYTRADER_SIGNALS_MQH
