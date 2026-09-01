#ifndef COPYTRADER_HTTP_MQH
#define COPYTRADER_HTTP_MQH

#include <JAson.mqh>

bool   g_bridge_sync_required      = false;
bool   g_bridge_last_request_ok    = false;
int    g_bridge_last_status_code   = 0;
string g_bridge_last_response_body = "";
string g_bridge_last_error         = "";

bool BridgeSyncRequired()
{
   return g_bridge_sync_required;
}

bool BridgeLastRequestOk()
{
   return g_bridge_last_request_ok;
}

int BridgeLastStatusCode()
{
   return g_bridge_last_status_code;
}

string BridgeLastResponseBody()
{
   return g_bridge_last_response_body;
}

string BridgeLastError()
{
   return g_bridge_last_error;
}

void ResetBridgeHttpState()
{
   g_bridge_sync_required      = false;
   g_bridge_last_request_ok    = false;
   g_bridge_last_status_code   = 0;
   g_bridge_last_response_body = "";
   g_bridge_last_error         = "";
}

bool ParseBridgeResponse(const string responseBody)
{
   if(responseBody == "")
   {
      Print("Bridge response parse skipped: empty body");
      return false;
   }

   string normalized = responseBody;
   StringToLower(normalized);

   StringReplace(normalized, " ", "");
   StringReplace(normalized, "\t", "");
   StringReplace(normalized, "\r", "");
   StringReplace(normalized, "\n", "");

   int sync_pos = StringFind(normalized, "\"sync_required\":");
   if(sync_pos < 0)
   {
      // Trade-signal replies may not include sync_required.
      // Do not spam normal logs for a successful response.
      return true;
   }

   g_bridge_sync_required =
      StringFind(normalized, "\"sync_required\":true") >= 0 ||
      StringFind(normalized, "\"sync_required\":\"true\"") >= 0 ||
      StringFind(normalized, "\"sync_required\":1") >= 0;

   // Health responses are silent when sync_required=false.
   // The EA will perform and log the actual sync action when this is true.
   return true;
}

bool SendToServer(string jsonData)
{
   char   post[];
   char   result[];
   string headers = "Content-Type: application/json\r\n";
   string response_headers = "";

   ResetBridgeHttpState();

   Print("DEBUG JSON -> ", jsonData);

   StringToCharArray(jsonData, post, 0, StringLen(jsonData));

   string url = BridgeServerURL + "/trade_signal";

   ResetLastError();
   int res = WebRequest(
      "POST",
      url,
      headers,
      RequestTimeout,
      post,
      result,
      response_headers
   );

   g_bridge_last_status_code = res;

   if(res == -1)
   {
      int error = GetLastError();
      g_bridge_last_error = IntegerToString(error);

      Print(
         "WebRequest error: ",
         error,
         ". Make sure URL is added to allowed URLs in Tools > Options > Expert Advisors"
      );
      return false;
   }

   int result_size = ArraySize(result);
   if(result_size > 0 && result[result_size - 1] == 0)
      result_size--;

   g_bridge_last_response_body = CharArrayToString(
      result,
      0,
      result_size,
      CP_UTF8
   );

   ParseBridgeResponse(g_bridge_last_response_body);

   if(res == 200)
   {
      g_bridge_last_request_ok = true;
      Print("Signal sent successfully to bridge server");
      return true;
   }

   Print("Bridge server returned status code: ", res);
   return false;
}

bool BridgeHealthCheck()
{
   char   post[];
   char   result[];
   string headers = "";
   string response_headers = "";
   string url = BridgeServerURL + "/health";

   ResetBridgeHttpState();
   ArrayResize(post, 0);

   ResetLastError();
   int res = WebRequest(
      "GET",
      url,
      headers,
      RequestTimeout,
      post,
      result,
      response_headers
   );

   g_bridge_last_status_code = res;

   if(res == -1)
   {
      int error = GetLastError();
      g_bridge_last_error = IntegerToString(error);

      Print(
         "Bridge health check WebRequest error: ",
         error,
         ". Make sure URL is added to allowed URLs in Tools > Options > Expert Advisors"
      );
      return false;
   }

   int result_size = ArraySize(result);
   if(result_size > 0 && result[result_size - 1] == 0)
      result_size--;

   g_bridge_last_response_body = CharArrayToString(
      result,
      0,
      result_size,
      CP_UTF8
   );

   ParseBridgeResponse(g_bridge_last_response_body);

   if(res == 200)
   {
      g_bridge_last_request_ok = true;

      // Silent for normal healthy responses.
      // Log only when the bridge requests an action.
      if(g_bridge_sync_required)
      {
         Print(
            "Bridge requests sync | body=",
            g_bridge_last_response_body
         );
      }

      return true;
   }

   Print("Bridge health returned status code: ", res);
   return false;
}

#endif // COPYTRADER_HTTP_MQH
