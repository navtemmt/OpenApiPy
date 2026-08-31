#ifndef COPYTRADER_SIGNALS_MQH
#define COPYTRADER_SIGNALS_MQH

bool GetSymbolTradeMeta(const string symbol,
                        double &contract_size,
                        double &vol_min,
                        double &vol_max,
                        double &vol_step,
                        double &tick_size,
                        double &tick_value,
                        double &point,
                        int &digits)
{
   contract_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   vol_min       = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   vol_max       = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   vol_step      = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   tick_size     = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   tick_value    = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   point         = SymbolInfoDouble(symbol, SYMBOL_POINT);
   digits        = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   if(contract_size <= 0.0)
   {
      Print("GetSymbolTradeMeta: invalid contract_size for ", symbol, " = ", contract_size);
      return false;
   }

   if(tick_size <= 0.0)
      Print("GetSymbolTradeMeta: warning invalid tick_size for ", symbol, " = ", tick_size);

   if(tick_value <= 0.0)
      Print("GetSymbolTradeMeta: warning invalid tick_value for ", symbol, " = ", tick_value);

   return true;
}

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

   double bid = 0.0;
   double ask = 0.0;
   SymbolInfoDouble(symbol, SYMBOL_BID, bid);
   SymbolInfoDouble(symbol, SYMBOL_ASK, ask);

   double contract_size = 0.0;
   double vol_min = 0.0;
   double vol_max = 0.0;
   double vol_step = 0.0;
   double tick_size = 0.0;
   double tick_value = 0.0;
   double point = 0.0;
   int digits = 0;

   GetSymbolTradeMeta(symbol,
                      contract_size,
                      vol_min,
                      vol_max,
                      vol_step,
                      tick_size,
                      tick_value,
                      point,
                      digits);

   string tradeType = (type == POSITION_TYPE_BUY) ? "BUY" : "SELL";
   int priceDigits = (digits > 0 ? digits : 5);

   string jsonData = "{"
      "\"action\":\"OPEN\","
      "\"ticket\":" + (string)ticket + ","
      "\"symbol\":\"" + JsonEscape(symbol) + "\","
      "\"type\":\"" + tradeType + "\","
      "\"volume\":" + DoubleToString(volume, 2) + ","
      "\"price\":" + DoubleToString(openPrice, priceDigits) + ","
      "\"entry_price\":" + DoubleToString(openPrice, priceDigits) + ","
      "\"current_bid\":" + DoubleToString(bid, priceDigits) + ","
      "\"current_ask\":" + DoubleToString(ask, priceDigits) + ","
      "\"sl\":" + DoubleToString(sl, priceDigits) + ","
      "\"tp\":" + DoubleToString(tp, priceDigits) + ","
      "\"magic\":" + (string)magic + ","
      "\"mt5_contract_size\":" + DoubleToString(contract_size, 8) + ","
      "\"mt5_volume_min\":" + DoubleToString(vol_min, 8) + ","
      "\"mt5_volume_max\":" + DoubleToString(vol_max, 8) + ","
      "\"mt5_volume_step\":" + DoubleToString(vol_step, 8) + ","
      "\"mt5_tick_size\":" + DoubleToString(tick_size, 10) + ","
      "\"mt5_tick_value\":" + DoubleToString(tick_value, 10) + ","
      "\"point\":" + DoubleToString(point, 10) + ","
      "\"digits\":" + IntegerToString(digits) +
      "}";

   SendToServer(jsonData);
   Print("Sent OPEN signal for ticket #", ticket, ": ", symbol, " ", tradeType, " ", volume);
}

void SendCloseSignal(long ticket, string symbol, double closedVolume, long magic)
{
   double contract_size = 0.0;
   double vol_min = 0.0;
   double vol_max = 0.0;
   double vol_step = 0.0;
   double tick_size = 0.0;
   double tick_value = 0.0;
   double point = 0.0;
   int digits = 0;

   if(symbol != "")
      GetSymbolTradeMeta(symbol,
                         contract_size,
                         vol_min,
                         vol_max,
                         vol_step,
                         tick_size,
                         tick_value,
                         point,
                         digits);

   string jsonData = "{"
      "\"action\":\"CLOSE\","
      "\"ticket\":" + (string)ticket + ",";

   if(symbol != "")
      jsonData += "\"symbol\":\"" + JsonEscape(symbol) + "\",";

   jsonData += "\"volume\":" + DoubleToString(closedVolume, 8);
   jsonData += ",\"magic\":" + (string)magic;

   if(symbol != "" && contract_size > 0.0)
   {
      jsonData += ",\"mt5_contract_size\":" + DoubleToString(contract_size, 8);
      jsonData += ",\"mt5_volume_min\":" + DoubleToString(vol_min, 8);
      jsonData += ",\"mt5_volume_max\":" + DoubleToString(vol_max, 8);
      jsonData += ",\"mt5_volume_step\":" + DoubleToString(vol_step, 8);
      jsonData += ",\"mt5_tick_size\":" + DoubleToString(tick_size, 10);
      jsonData += ",\"mt5_tick_value\":" + DoubleToString(tick_value, 10);
      jsonData += ",\"point\":" + DoubleToString(point, 10);
      jsonData += ",\"digits\":" + IntegerToString(digits);
   }

   jsonData += "}";

   SendToServer(jsonData);
   Print("Sent CLOSE signal for ticket #", ticket,
         " symbol=", symbol, " closedVolume=", closedVolume, " magic=", magic);
}

void SendModifySignal(ulong ticket, double sl, double tp, long magic)
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
      "\"tp\":" + DoubleToString(tp, 5) + ","
      "\"magic\":" + (string)magic +
      "}";

   SendToServer(jsonData);
   Print("Sent MODIFY signal for ticket #", ticket, ": ", symbol,
         " SL=", sl, " TP=", tp, " magic=", magic);
}

#endif // COPYTRADER_SIGNALS_MQH
