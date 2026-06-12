import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../../environments/environment';

export interface ChatTurn {
  role: 'user' | 'assistant';
  text: string;
}

export interface ChatSource {
  title: string;
  url?: string;
}

export interface ChatResponse {
  reply: string;
  sources?: ChatSource[];
}

@Injectable({ providedIn: 'root' })
export class AiService {
  private http = inject(HttpClient);
  private endpoint = `${environment.apiBaseUrl}/ai/chat/`;

  ask(message: string, history: ChatTurn[]): Observable<ChatResponse> {
    return this.http.post<ChatResponse>(this.endpoint, { message, history });
  }
}
