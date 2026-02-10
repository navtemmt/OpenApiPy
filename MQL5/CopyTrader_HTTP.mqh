#pragma once

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
