#ifndef COPYTRADER_SIGNALS_MQH
#define COPYTRADER_SIGNALS_MQH

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
      "\"symbol\":\"" + symbol + "\","
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

void SendCloseSignal(long ticket, string symbol, double closedVolume)
{
   double contract_size = 0.0, vol_min = 0.0, vol_max = 0.0, vol_step = 0.0;
   if(symbol != "")
      GetSymbolTradeMeta(symbol, contract_size, vol_min, vol_max, vol_step);

   string jsonData = "{"
      "\"action\":\"CLOSE\","
      "\"ticket\":" + (string)ticket + ",";

   if(symbol != "")
      jsonData += "\"symbol\":\"" + symbol + "\",";

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
      jsonData += "\"symbol\":\"" + symbol + "\",";

   jsonData +=
      "\"sl\":" + DoubleToString(sl, 5) + ","
      "\"tp\":" + DoubleToString(tp, 5) +
      "}";

   SendToServer(jsonData);
   Print("Sent MODIFY signal for ticket #", ticket, ": ", symbol, " SL=", sl, " TP=", tp);
}

#endif // COPYTRADER_SIGNALS_MQH
