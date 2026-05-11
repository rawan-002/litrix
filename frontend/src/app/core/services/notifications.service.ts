import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { tap } from 'rxjs';
import { environment } from '../../../environments/environment';


export interface Notification {
  NotificationID: number;
  Type: string;
  Title: string;
  Message: string;
  Metadata: any;
  IsRead: boolean;
  CreatedAt: string;
  ReadAt: string | null;
}


@Injectable({ providedIn: 'root' })
export class NotificationsService {
  private http = inject(HttpClient);
  private API = `${environment.apiBaseUrl}/auth`;

  readonly notifications = signal<Notification[]>([]);
  readonly unreadCount = signal(0);

  load(unreadOnly = false) {
    const url = `${this.API}/notifications/${unreadOnly ? '?unread=true' : ''}`;
    return this.http.get<{ notifications: Notification[]; unread_count: number }>(url).pipe(
      tap(res => {
        this.notifications.set(res.notifications || []);
        this.unreadCount.set(res.unread_count || 0);
      }),
    );
  }

  markRead(id: number) {
    return this.http.post(`${this.API}/notifications/${id}/read/`, {}).pipe(
      tap(() => {
        this.notifications.update(arr =>
          arr.map(n => n.NotificationID === id ? { ...n, IsRead: true } : n),
        );
        this.unreadCount.update(c => Math.max(0, c - 1));
      }),
    );
  }

  markAllRead() {
    return this.http.post(`${this.API}/notifications/mark-all-read/`, {}).pipe(
      tap(() => {
        this.notifications.update(arr => arr.map(n => ({ ...n, IsRead: true })));
        this.unreadCount.set(0);
      }),
    );
  }
}
